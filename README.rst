Zuul Queue Status WebApp
=========================

A simple webapp to query the OpenStack Zuul status endpoint and display
information about the jobs.  It was initially designed for monitoring the
check-tripleo queue, but that queue no longer exists.  However, it
has evolved into a useful general purpose monitoring tool.  For example,
it is possible to monitor non-OpenStack Zuuls too.  There is an example of
the parameters to pass for that use case in the default queue links.

See a live example of the tool at `http://zuul-status.nemebean.com/ <http://zuul-status.nemebean.com/>`_
