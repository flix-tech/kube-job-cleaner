#!/usr/bin/env python3

import argparse
import datetime
import os
import pykube
import time


def parse_time(s: str):
    return datetime.datetime.strptime(s, '%Y-%m-%dT%H:%M:%SZ').replace(tzinfo=datetime.timezone.utc).timestamp()


parser = argparse.ArgumentParser()
parser.add_argument('--timeout-finished-seconds', type=int, default=3600, help='Delete all finished jobs older than ..')
parser.add_argument('--timeout-all-seconds', type=int, default=-1, help='Kill all jobs older than ..')
parser.add_argument('--dry-run', action='store_true', help='Dry run mode')
args = parser.parse_args()

def create_api():
    try:
        config = pykube.KubeConfig.from_service_account()
    except FileNotFoundError:
        # local testing
        config = pykube.KubeConfig.from_file(os.path.expanduser('~/.kube/config'))
    return pykube.HTTPClient(config)

now = time.time()



def delete_job(job,seconds_since_completion, reason=""):
    """
    Deletes the job and all pods created by it
    :param job:
    :return:
    """
    jobuid = job.obj['metadata']['uid']
    print('Deleting job {} ({:.0f}s old) {} ..'.format(job.name, seconds_since_completion, reason))
    if args.dry_run:
        print('** DRY RUN **')
    else:
        job.delete()
    for pod in pykube.Pod.objects(api, namespace=pykube.all).filter(selector="controller-uid = {}".format(jobuid)):
        pod.delete()

def delete_jobs_after_timeout(api):
    """
    :param api:
    :return:
    """
    for job in pykube.Job.objects(api, namespace=pykube.all):
        completion_time = job.obj['status'].get('completionTime')
        status = job.obj['status']
        # is job finished?
        if (status.get('succeeded') or status.get('failed')) and completion_time:
            completion_time = parse_time(completion_time)
            seconds_since_completion = now - completion_time
            if seconds_since_completion > args.timeout_finished_seconds:
                # Use case #3
                delete_job(job,seconds_since_completion, reason="Job has been finished too long")
        else:
            start_time = parse_time(job.obj['status'].get('startTime'))
            seconds_since_start = now - start_time
            annotations = job.obj['metadata'].get('annotations')
            # Determine the timeout in seconds for this job
            timeout_jobs = args.timeout_all_seconds
            if annotations is not None:
                timeout_override = annotations.get('cleanup-timeout')
                if timeout_override is not None:
                    timeout_jobs = int(timeout_override)
            # Check whether a timeout is active for this job.
            if timeout_jobs < 0:
                continue
            if start_time and seconds_since_start > timeout_jobs:
                delete_job(job,seconds_since_start, reason="Job ran too long")


def old_stopped_pods(api):
    """
    Finds stopped(all containers!) pods that exceed the maximum age.
    Uses annotation flag "cleanup-finished" to determine, whether timeout should apply to regular pods.
    :return:
    """
    for pod in pykube.Pod.objects(api, namespace=pykube.all):
        # Finished (because it was part of a job) or opted into garbage collection
        if pod.obj['status'].get('phase') in ('Succeeded', 'Failed') or ('cleanup-finished' in pod.obj['metadata'].get('annotations')):
            seconds_since_completion = 0
            if pod.obj['status'].get('containerStatuses') is None:
                print("Warning: Skipping pod without containers ({})".format(pod.obj['metadata'].get('name')))
                continue
            for container in pod.obj['status'].get('containerStatuses'):
                if 'terminated' in container['state']:
                    state = container['state']
                elif 'terminated' in container.get('lastState', {}):
                    # current state might be "waiting", but lastState is good enough
                    state = container['lastState']
                else:
                    state = None
                if state:
                    finish = now - parse_time(state['terminated']['finishedAt'])
                    if seconds_since_completion == 0 or finish < seconds_since_completion:
                        seconds_since_completion = finish
            if seconds_since_completion > args.timeout_finished_seconds:
                yield pod, seconds_since_completion


if __name__ == '__main__':
    api = create_api()
    delete_jobs_after_timeout(api)
    # Usecase #1
    for pod, seconds_since_completion in old_stopped_pods(api):
        print('Deleting {} ({:.0f}s old)..'.format(pod.name, seconds_since_completion))
        if args.dry_run:
            print('** DRY RUN **')
        else:
            pod.delete()


    current_jobs_uids = set()
    for job in pykube.Job.objects(api, namespace=pykube.all):
        current_jobs_uids.add(job.obj["metadata"]["uid"])
    for pod in pykube.Pod.objects(api, namespace=pykube.all).filter(selector="job-name"):
        if pod.obj['metadata']['labels']['controller-uid'] not in current_jobs_uids:
            pod.delete()
    print("Finished")
    time.sleep(10000)