#!/usr/bin/env python
#    Copyright 2017 Red Hat, Inc.
#    Licensed under the Apache License, Version 2.0 (the "License"); you may
#    not use this file except in compliance with the License. You may obtain
#    a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
#    WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
#    License for the specific language governing permissions and limitations
#    under the License.

import os

# When we're running locally we don't need to do this.
if 'OPENSHIFT_PYTHON_DIR' in os.environ:
    virtenv = os.environ['OPENSHIFT_PYTHON_DIR'] + '/virtenv/'
    virtualenv = os.path.join(virtenv, 'bin/activate_this.py')
    try:
        execfile(virtualenv, dict(__file__=virtualenv))
    except IOError:
        pass

# requires: pyramid, jinja2
import cStringIO
import gzip
import json
import sys
import time
import urllib2
import yaml
# hack to make sure we can load wsgi.py as a module in this class
sys.path.insert(0, os.path.dirname(__file__))

import jinja2
from pyramid import config
from pyramid import renderers
from pyramid import response
from pyramid import view
from wsgiref.simple_server import make_server

# This is the ci total runtime reported in Graphite, which only includes the
# tripleo deployment bits
GRAPHITE_TIME_HOURS = 2.22
# Add about 15 minutes for the stuff that happens before and after
JOB_TIME_HOURS = GRAPHITE_TIME_HOURS + .25
TRIPLEO_TEST_CLOUDS = ['tripleo-test-cloud-rh1']
# Base color codes
RED='be1400'
GREEN='008800'
BLUE='1400be'

max_jobs_last_update = 0
max_jobs_cache = 0


class DataRetrievalFailed(Exception):
    pass


def _get_remote_data(address, datatype='json'):
    req = urllib2.Request(address)
    req.add_header('Accept-encoding', 'gzip')
    try:
        remote_data = urllib2.urlopen(req, timeout=10)
    except Exception as e:
        msg = 'Failed to retrieve data from %s: %s' % (address, str(e))
        raise DataRetrievalFailed(msg)
    data = ""
    while True:
        chunk = remote_data.read()
        if not chunk:
            break
        data += chunk

    if remote_data.info().get('Content-Encoding') == 'gzip':
        buf = cStringIO.StringIO(data)
        f = gzip.GzipFile(fileobj=buf)
        data = f.read()

    if datatype == 'json':
        return json.loads(data)
    else:
        return yaml.safe_load(data)


def _get_zuul_status():
    return _get_remote_data('http://zuulv3.openstack.org/status.json')


def _get_max_jobs():
    """Read max jobs from the nodepool config"""
    # Only refresh data every 30 minutes
    global max_jobs_last_update, max_jobs_cache
    if time.time() - max_jobs_last_update < 30 * 60:
        return max_jobs_cache

    print 'Refreshing nodepool data'
    data = _get_remote_data('http://git.openstack.org/cgit/openstack-infra/project-config/plain/nodepool/nl01.openstack.org.yaml',
                            'yaml')
    providers = data['providers']
    max_jobs = 0
    for cloud in TRIPLEO_TEST_CLOUDS:
        current = [c for c in providers if c['name'] == cloud][0]
        max_jobs += current['pools'][0]['max-servers']
    max_jobs_last_update = time.time()
    max_jobs_cache = max_jobs
    return max_jobs


def _format_time(ms):
    if ms is None:
        return '??:??'
    s = ms / 1000
    m = s / 60
    h, m = divmod(m, 60)
    return '%02d:%02d' % (h, m)


def process_request(request):
    """Return the appropriate response data for a request

    Returns a tuple of (template, params) representing the appropriate
    data to put in a response to request.
    """
    loader = jinja2.FileSystemLoader('templates')
    env = jinja2.Environment(loader=loader)
    t = env.get_template('zuul-status.jinja2')

    try:
        zuul_data = _get_zuul_status()
        max_jobs = _get_max_jobs()
    except Exception as e:
        values = {'error': str(e)}
        return t, values
    queue_name = request.params.get('queue', 'check-tripleo')
    pipeline = [p for p in zuul_data['pipelines']
                if p['name'] == queue_name][0]
    counter = 0
    job_counter = 0
    running = 0
    queued = 0
    complete = 0
    queue_time = 0
    values = {}
    values['changes'] = []
    for change in pipeline['change_queues']:
        if len(change['heads']) == 0:
            continue
        counter += 1
        queue_counter = 0
        all_heads = []
        for h in change['heads']:
            all_heads += h
        for data in all_heads:
            if len(all_heads) > 1:
                queue_counter += 1
            url = data['url']
            # Read it once so it's consistent
            current_time = time.time()
            start_time = data['enqueue_time'] or current_time * 1000
            total = current_time * 1000 - start_time
            total = _format_time(total)
            change_data = {'number': counter,
                           'queue_number': queue_counter,
                           'total': total,
                           'id': data['id'],
                           'url': url,
                           'project': data['project'],
                           'user': data['owner']['username'],
                           }

            change_data['jobs'] = []
            for job in data['jobs']:
                color = BLUE
                weight = 'normal'
                link = job['url'] or ''
                if job['elapsed_time'] is not None:
                    result = job['result']
                    color = GREEN
                    if result is not None:
                        if result == 'FAILURE':
                            color = RED
                        weight = 'bold'
                        link = job['report_url']
                        complete += 1
                    else:
                        running += 1
                else:
                    queued += 1
                shortname = job['name']
                if 'centos-7-' in job['name']:
                    shortname = shortname.split('centos-7-')[1]
                elapsed = _format_time(job['elapsed_time'])
                style = 'color: %s; font-weight: %s' % (color, weight)
                if queue_name == 'check-tripleo':
                    # At max capacity, a job should finish once per completion_rate
                    # minutes on average.
                    completion_rate = 60. / (float(max_jobs) / JOB_TIME_HOURS)
                    if not job['elapsed_time']:
                        queue_time = int((job_counter - complete) * completion_rate * 60 * 1000)
                    else:
                        if job['result']:
                            queue_time = 0
                        else:
                            queue_time = JOB_TIME_HOURS * 60 * 60 * 1000 - job['elapsed_time']
                else:
                    if job['result']:
                        queue_time = 0
                    elif job['estimated_time'] and job['elapsed_time']:
                        queue_time = job['estimated_time'] * 1000 - job['elapsed_time']
                    else:
                        queue_time = None
                # Estimated time to complete
                if queue_time is not None:
                    etc = _format_time(max(queue_time, 0))
                else:
                    etc = '??:??'
                job_counter += 1
                job_data = {'number': job_counter,
                            'elapsed': elapsed,
                            'etc': etc,
                            'name': shortname,
                            'link': link,
                            'style': style,
                            }
                change_data['jobs'].append(job_data)
            values['changes'].append(change_data)
    values['running'] = running
    values['max_jobs'] = int(max_jobs)
    values['queued'] = queued
    values['complete'] = complete
    values['active'] = running + queued
    values['total'] = running + queued + complete
    values['queue_time'] = _format_time(queue_time)
    values['queue_name'] = queue_name
    values['job_red'] = RED
    values['job_green'] = GREEN
    values['job_blue'] = BLUE

    return t, values


@view.view_config(route_name='zuul_status')
def zuul_status(request):
    template, params = process_request(request)
    return response.Response(template.render(**params))

if __name__ == '__main__':
    conf = config.Configurator()
    conf.add_route('zuul_status', '/')
    conf.scan()
    app = conf.make_wsgi_app()
    ip = os.environ['OPENSHIFT_PYTHON_IP']
    port = int(os.environ['OPENSHIFT_PYTHON_PORT'])
    server = make_server(ip, port, app)
    server.serve_forever()

