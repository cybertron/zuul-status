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

# requires: pyramid, jinja2, psutil, matplotlib
import collections
import copy
import cStringIO
import datetime
import gzip
import json
import sys
import time
import urllib2
import yaml
# hack to make sure we can load wsgi.py as a module in this class
sys.path.insert(0, os.path.dirname(__file__))

import jinja2
import matplotlib
matplotlib.use('Agg')
from matplotlib import pyplot
import psutil
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
OPENSTACK_ZUUL = 'http://zuul.openstack.org/api/status'
# Base color codes
RED='be1400'
GREEN='008800'
BLUE='1400be'
KNOWN_QUEUES = ['gate', 'check', 'experimental', 'check-openstack']

max_jobs_last_update = 0
max_jobs_cache = 0

total_template = {'running': 0, 'queued': 0, 'complete': 0}
job_total = {'gate': copy.copy(total_template),
             'check': copy.copy(total_template),
             'experimental': copy.copy(total_template),
             'timestamp': None,
             }
job_totals = collections.deque(maxlen=1000)


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


def _get_zuul_status(zuul_addr=OPENSTACK_ZUUL):
    return _get_remote_data(zuul_addr)


def _format_time(ms):
    if ms is None:
        return '??:??'
    s = ms / 1000
    m = s / 60
    h, m = divmod(m, 60)
    return '%02d:%02d' % (h, m)


def matches_filter(job, change_data, filter_text):
    if not filter_text:
        return True
    return (filter_text in job.get('name', '') or
            filter_text in change_data['id'] or
            filter_text in change_data['project'] or
            filter_text in change_data['user']
            )


def process_request(request):
    """Return the appropriate response data for a request

    Returns a tuple of (template, params) representing the appropriate
    data to put in a response to request.

    This handles requests for a list of changes.
    """
    loader = jinja2.FileSystemLoader('templates')
    env = jinja2.Environment(loader=loader)
    t = env.get_template('zuul-status.jinja2')
    zuul_addr = request.params.get('zuul', OPENSTACK_ZUUL)

    try:
        zuul_data = _get_zuul_status(zuul_addr)
        queue_name = request.params.get('queue', 'gate')
        filter_text = request.params.get('filter', '')
        queue_names = KNOWN_QUEUES
        if queue_name != 'all':
            queue_names = [queue_name]
        pipelines = [p for p in zuul_data['pipelines']
                    if p['name'] in queue_names]
    except Exception as e:
        values = {'error': repr(e)}
        return t, values
    counter = 0
    job_counter = 0
    running = 0
    queued = 0
    complete = 0
    queue_time = 0
    values = {}
    values['changes'] = []
    for pl in pipelines:
        for change in pl['change_queues']:
            if len(change['heads']) == 0:
                continue
            counter += 1
            queue_counter = 0
            all_heads = []
            for h in change['heads']:
                # h is a list, we're just concatenating all of them
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
                if data['owner']:
                    user = data['owner']['name']
                    if 'username' in data['owner']:
                        user += ' (%s)' % data['owner']['username']
                else:
                    user = 'None'
                change_data = {'number': counter,
                            'queue_number': queue_counter,
                            'total': total,
                            'id': data['id'],
                            'url': url,
                            'project': data['project'],
                            'user': user,
                            }
                if not matches_filter({}, change_data, filter_text):
                    continue

                change_data['jobs'] = []
                for job in data['jobs']:
                    if not matches_filter(job, change_data, filter_text):
                        continue
                    color = BLUE
                    weight = 'normal'
                    link = job['url'] or ''
                    if job['elapsed_time'] is not None:
                        result = job['result']
                        color = GREEN
                        if result is not None:
                            color = RED
                            if result == 'SUCCESS':
                                color = GREEN
                            weight = 'bold'
                            link = job['report_url']
                            complete += 1
                        else:
                            running += 1
                    else:
                        queued += 1
                    # Relative links need to be rewritten to point at the zuul server
                    if not (link.startswith('http') or link.startswith('telnet')):
                        link = 'http://zuul.openstack.org/%s' % link
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
    values['queued'] = queued
    values['complete'] = complete
    values['active'] = running + queued
    values['total'] = running + queued + complete
    values['queue_time'] = _format_time(queue_time)
    values['queue_name'] = queue_name
    values['job_red'] = RED
    values['job_green'] = GREEN
    values['job_blue'] = BLUE
    values['filter_text'] = filter_text
    calculate_uptime(values)
    if zuul_addr != OPENSTACK_ZUUL:
        values['zuul'] = zuul_addr

    return t, values


def calculate_uptime(values):
    p = psutil.Process(os.getpid())
    uptime_seconds = int(time.time() - p.create_time())
    uptime = datetime.timedelta(seconds=uptime_seconds)
    values['app_uptime'] = str(uptime)


def process_graphs(request):
    """Return the appropriate response data for a request

    Returns a tuple of (template, params) representing the appropriate
    data to put in a response to request.

    This handles requests for graphs of job counts.
    """
    loader = jinja2.FileSystemLoader('templates')
    env = jinja2.Environment(loader=loader)
    t = env.get_template('queue-graphs.jinja2')
    zuul_addr = request.params.get('zuul', OPENSTACK_ZUUL)
    values = {}

    force = False
    if len(job_totals):
        last = job_totals[-1]
        if len(job_totals) < 2:
            force = True
    else:
        force = True
    # I have a cron job set up to hit this endpoint every 5 minutes, so I'm
    # setting the refresh time to 4 so it will always refresh.
    if (force or datetime.datetime.utcnow() - last['timestamp'] >
            datetime.timedelta(minutes=4)):
        new_total = copy.deepcopy(job_total)
        new_total['timestamp'] = datetime.datetime.utcnow()

        try:
            zuul_data = _get_zuul_status(zuul_addr)
        except Exception as e:
            values = {'error': repr(e)}
            return t, values

        for queue in KNOWN_QUEUES[:3]:
            pipelines = [p for p in zuul_data['pipelines']
                        if p['name'] == queue]
            for pl in pipelines:
                for change in pl['change_queues']:
                    if len(change['heads']) == 0:
                        continue
                    all_heads = []
                    for h in change['heads']:
                        # h is a list, we're just concatenating all of them
                        all_heads += h
                    for data in all_heads:
                        for job in data['jobs']:
                            if job['elapsed_time'] is not None:
                                if job['result'] is not None:
                                    new_total[queue]['complete'] += 1
                                else:
                                    new_total[queue]['running'] += 1
                            else:
                                new_total[queue]['queued'] += 1

        # TODO: persist this data on disk somewhere
        job_totals.append(new_total)

    create_graph(KNOWN_QUEUES[:3],
                 ['queued', 'running', 'complete'],
                 values,
                 'all_data',
                 'All')
    create_graph(['gate'],
                 ['queued', 'running', 'complete'],
                 values,
                 'gate_data',
                 'Gate')
    create_graph(['check'],
                 ['queued', 'running', 'complete'],
                 values,
                 'check_data',
                 'Check')

    calculate_uptime(values)

    return t, values


def create_graph(queues, types, values, name, title):
    pyplot.figure(figsize=(13, 5))
    for queue in queues:
        for t in types:
            x = matplotlib.dates.date2num(
                [(i['timestamp']) for i in job_totals])
            y = [i[queue][t] for i in job_totals]
            pyplot.plot_date(x, y, label='%s-%s' % (queue, t),
                             linestyle='solid',
                             marker='None')
            pyplot.xlabel('time')
            pyplot.ylabel('count')
    pyplot.legend()
    pyplot.title(title)
    img = cStringIO.StringIO()
    pyplot.savefig(img, format='svg')
    pyplot.close()
    values[name] = img.getvalue().decode('utf-8')


@view.view_config(route_name='zuul_status')
def zuul_status(request):
    template, params = process_request(request)
    return response.Response(template.render(**params))


@view.view_config(route_name='queue_graphs')
def queue_graphs(request):
    template, params = process_graphs(request)
    return response.Response(template.render(**params))


conf = config.Configurator()
conf.add_route('zuul_status', '/')
conf.add_route('queue_graphs', '/graphs')
conf.scan()
app = conf.make_wsgi_app()
if __name__ == '__main__':
    try:
        ip = os.environ['OPENSHIFT_PYTHON_IP']
        port = int(os.environ['OPENSHIFT_PYTHON_PORT'])
    except KeyError:
        # OpenShift 3 doesn't believe in backwards compatibility
        ip = '0.0.0.0'
        port = 8080
    server = make_server(ip, port, app)
    server.serve_forever()

