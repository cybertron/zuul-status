Zuul Queue Status WebApp
=========================

A simple webapp to query the OpenStack Zuul status endpoint and display
information about the jobs.  It was designed specifically for me to keep
an eye on the check-tripleo queue, so some of the behavior is specific to
that.  In particular the estimated queue time is based on the average time
it takes to run a tripleo ovb job.  It is also based on the assumption of
a full queue, but if the queue is not full then the completion time is just
however long it takes the job to run.

There are some other limitations as well (the gate queue does not work well,
for example), but it suits my current purposes so I haven't been motivated
to address them.

See a live example of the tool at `http://zuul-status.nemebean.com/ <http://zuul-status.nemebean.com/>`_
