
from Queue import Queue
from uuid import uuid4
import logging
import json

logger = logging.getLogger(__name__)

from circuits import Component, Event, handler
from circuits.core.timers import Timer
from circuits.web.client import Client, Request
from bson import BSON, json_util

from arke.spool import Spooler

IDLE = object()
RETRY_INTERVAL_CAP = 300
CHECK_INTERVAL = 30

class Retry(Event): pass

def request_factory(hostname, sourcetype, timestamp, data, extra):
    path = '/store/%s/%s/%f' % (hostname, sourcetype, timestamp)

    headers = {'Content-type': 'application/bson'}
    if extra and isinstance(extra, dict):
        headers['extra'] = json.dumps(extra, default=json_util.default)

    method = 'PUT'
    body = BSON.encode(data)

    return Request(method, path, body, headers)


class Persist(Component):

    def __init__(self, pool_count=10):
        super(Persist, self).__init__()
        self._pool_count = pool_count
        self.queue = Queue()
        self.hostname = self.root.config.get('core', 'hostname')

    def started(self, *args, **kwargs):
        self.spool = Spooler().register(self)
        b = self.root.config.get('core', 'persist_backend')
        b = b.lower()
        h = self.root.config.get('backend:%s' % b, 'host')
        p = self.root.config.get('backend:%s' % b, 'port')
        if b in ('http', 'https'):
            self._backends = backends = {}
            self._backend_state = {}
            while len(self._backends) < self._pool_count:
                bid = uuid4().hex
                backends[bid] = RetryHTTPClient(host=h, port=p, scheme=b, channel=bid).register(self)
                self._backend_state[bid] = IDLE
        else:
            logger.error("Invalid backend given: %s" % b)
            raise SystemExit
        Timer(CHECK_INTERVAL, Event(), 'persist', t=self, persist=True).register(self)

    @handler('response_success')
    def _on_response(self, bid):
        rid = self._backend_state[bid]
        self.fire(Event(rid), 'remove', target=self.spool)
        self.spool.commit(rid)
        self._backend_state[bid] = IDLE
        self.fire(Event(bid), 'persist', target=self)


    def persist(self, bid=None, rid=None):
        if rid is None and self.queue.empty():
            #nothing to persist.
            return
        if bid is None:
            available_backends = [b for b,s in self._backend_state.iteritems() if s is IDLE]
            if not available_backends:
                if rid is not None:
                    self.queue.put(rid[0])
                #sleep and try again?
                return
            bid = available_backends[0]

        if rid is None:
            rid = self.queue.get()
            (sourcetype, timestamp, data, extra) = self.spool.get(rid)
        else:
            (sourcetype, timestamp, data, extra) = rid[1]
            rid = rid[0]

        self._backend_state[bid] = rid
        self.fire(request_factory(
            self.hostname, sourcetype, timestamp, data, extra),
            'request', target=bid
        )



class HTTPClient(Client):
    def __init__(self, host, port, scheme, channel= None):
        url = '%s://%s:%s/' % (scheme, host, port)
        if channel is None:
            channel = self.channel
        super(HTTPClient, self).__init__(url, channel=channel)

class RetryHTTPClient(HTTPClient):
    def __init__(self, host, port, scheme, channel= None):
        super(RetryHTTPClient, self).__init__(host, port, scheme, channel=channel)
        self._prev_request = None

    @handler('request', priority=1)
    def on_request(self, *args, **kwargs):
        self._prev_request = (args, kwargs)
        self._attempt = 0
        self.connect()

    @handler('retry')
    def retry(self):
        self.connect()
        args, kwargs = self._prev_request
        self.request(*args, **kwargs)

    @handler('response', priority=-1)
    def on_response(self, response):
        if response.status in (200,204):
            return self.fire(Event(self.channel), 'response_success')

        secs = self._attempt * 2
        self._attempt += 1
        if secs < 1:
            secs = .2
        elif secs > RETRY_INTERVAL_CAP:
            secs = RETRY_INTERVAL_CAP
        Timer(secs, Retry(), t=self).register(self)


