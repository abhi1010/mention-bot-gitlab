#!/usr/bin/env python
# coding: utf-8
import os
import json
import logging
from queue import Queue, Empty
from threading import Thread
import datetime
import math
import time
import argparse
import copy

from flask import Flask, request

## setup logging

parser = argparse.ArgumentParser()
subparsers = parser.add_subparsers(help='Startup Mode')
group = parser.add_mutually_exclusive_group()
group.add_argument('-listen', action='store_true', default=False)
group.add_argument('-quick-check', action='store_true', default=False)

args = parser.parse_args() if __name__ == '__main__' else None

import logging.config
logging.config.dictConfig({
    'version': 1,
    'disable_existing_loggers': False,
    'formatters': {
        'basic': {
            'format':
            '%(levelname)s %(asctime)s %(filename)s:%(lineno)d '
            '%(message)s'
        },
    },
    'handlers': {
        'console': {
            'level': 'DEBUG',
            'class': 'logging.StreamHandler',
            'formatter': 'basic'
        },
        'file': {
            'class': 'logging.handlers.RotatingFileHandler',
            'formatter': 'basic',
            'filename': '/tmp/mention-bot-hook.log'
            if args and args.listen else '/tmp/mention-bot-checks.log',
            'maxBytes': 100*1024*1024,
            'backupCount': 30
        }
    },
    'root': {
        'level': 'INFO',
        'handlers': ['console', 'file']
    }
})
logger = logging.getLogger()

## end logging setup
from mention import gitlab_client
from mention import mention_bot
from mention import config
from mention import helper

app = Flask(__name__)
_STOP_PROCESS = False
enclosure_queue = Queue()


@app.route('/check_health', methods=['GET'])
def check_health():
    return "mention-bot"


@app.route('/', methods=['GET'])
def mentionbot():
    return "Gitlab Mention Bot active"


@app.route('/', methods=['POST'])
def webhook():
    event = request.headers.get('X-Gitlab-Event')
    if not event:
        return '', 400
    if event != 'Merge Request Hook':
        return '', 200

    # add payload to queue so that _payload_worker(q) can process it in
    # a separate thread
    payload = json.loads(request.data)
    enclosure_queue.put((datetime.datetime.now(), payload))

    return "", 200


def _manage_payload(payload):
    logger.info('_' * 80)
    logger.info('Received payload<{}>: {}'.format(
        id(payload), helper.load_dict_as_yaml(payload)))
    username = payload['user']['username']
    project_id = payload['object_attributes']['target_project_id']
    target_branch = payload['object_attributes']['target_branch']
    namespace = payload['object_attributes']['target']['path_with_namespace']
    merge_request_id = payload['object_attributes']['iid']
    # loading config
    logger.info('Current Action={}'.format(
        payload['object_attributes']['action']))
    try:
        cfg = mention_bot.get_repo_config(project_id, target_branch,
                                          config.CONFIG_PATH)

        diff_files = mention_bot.get_diff_files(project_id, merge_request_id)
        logging.info(
            f'PiD: {project_id}, IID: {merge_request_id}; files={diff_files}')

        if mention_bot.is_valid(cfg, payload):
            owners = mention_bot.guess_owners_for_merge_reqeust(
                project_id, namespace, target_branch, merge_request_id,
                username, cfg, diff_files)
            if owners:
                logging.info(f'owners = {owners}; username={username}')
                mention_bot.add_comment(project_id, merge_request_id, username,
                                        owners, cfg)
            else:
                logging.info(f'No Owners found: PiD:{project_id}; MID:{merge_request_id}, username: {username}')

        if payload['object_attributes']['action'] in [
                'open', 'reopen', 'closed', 'close', 'merge'
        ]:
            mention_bot.manage_labels(payload, project_id, merge_request_id,
                                      cfg, diff_files)
    except gitlab_client.ConfigSyntaxError as e:
        gitlab_client.add_comment_merge_request(project_id, merge_request_id,
                                                e.message)


def _check_and_sleep(ts):
    now = datetime.datetime.now()
    exp_ts = datetime.timedelta(seconds=10) + ts
    if exp_ts > now:
        should_wait = math.ceil((exp_ts - now).total_seconds())
        if should_wait:
            logger.info('ts={}; now={}; sleeping for: {}'.format(
                ts, now, should_wait))
            time.sleep(should_wait)


def _payload_worker(q):
    # this worker is needed solely because sometimes the MR comes in too fast,
    # and gitlab queries fail. So let's add a delay of 10s, to ensure that
    # all updates work.
    logger.info('Looking for next payload')
    global _STOP_PROCESS
    while not _STOP_PROCESS:
        try:
            payload_ts, payload = q.get(timeout=2)
            logger.info('Looking for next payload')
            logger.info('Payload found: at ts={}; id={}'.format(
                payload_ts, id(payload)))
            _check_and_sleep(payload_ts)
            try:
              _manage_payload(payload)
            except Exception as e:
              logger.error(f'Exception with the message: {str(e)}')
            q.task_done()
        except Empty:
            pass

def main():
    # setup thread to handle the payloads
    worker = Thread(target=_payload_worker, args=(enclosure_queue, ))
    worker.setDaemon(True)
    worker.start()

    app.run(host='0.0.0.0')
    global _STOP_PROCESS
    _STOP_PROCESS = True
    logger.info('Stopping worker...')
    worker.join()
    logger.info('worker stopped...')


if __name__ == '__main__':
    logger.info(f'args = {args}...')
    config.check_config()
    if args.listen:
        main()
    if args.quick_check:
        mention_bot.check_merge_requests('p/higgs')
