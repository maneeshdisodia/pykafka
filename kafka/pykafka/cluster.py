import logging
import time

from .broker import Broker
from .topic import Topic
from .protocol import ConsumerMetadataRequest, ConsumerMetadataResponse


logger = logging.getLogger(__name__)


class Cluster(object):
    """Cluster implementation used to populate the KafkaClient."""

    def __init__(self, hosts, handler, timeout):
        self._seed_hosts = hosts
        self._timeout = timeout
        self._handler = handler
        self._brokers = {}
        self._topics = {}
        self.update()

    @property
    def brokers(self):
        return self._brokers

    @property
    def topics(self):
        return self._topics

    def _get_metadata(self):
        """Get fresh cluster metadata from a broker"""
        # Works either on existing brokers or seed_hosts list
        if self.brokers:
            brokers = self.brokers.values()
        else:
            brokers = self._seed_hosts.split(',')

        for broker in brokers:
            try:
                if isinstance(broker, basestring):
                    h, p = broker.split(':')
                    broker = Broker(-1, h, p, self._handler, self._timeout)
                return broker.request_metadata()
            # TODO: Change to typed exception
            except Exception:
                logger.exception('Unable to connect to broker %s', broker)
                raise
        raise Exception('Unable to connect to a broker to fetch metadata.')

    def _update_brokers(self, broker_metadata):
        """Update brokers with fresh metadata.

        :param broker_metadata: Metadata for all brokers
        :type broker_metadata: Dict of `{name: metadata}` where `metadata is
            :class:`kafka.pykafka.protocol.BrokerMetadata`
        """
        # FIXME: A cluster with no topics returns no brokers in metadata
        # Remove old brokers
        removed = set(self._brokers.keys()) - set(broker_metadata.keys())
        for id_ in removed:
            logger.info('Removing broker %s', self._brokers[id_])
            self._brokers.pop(id_)
        # Add/update current brokers
        for id_, meta in broker_metadata.iteritems():
            if id_ not in self._brokers:
                logger.info('Adding new broker %s:%s', meta.host, meta.port)
                self._brokers[id_] = Broker.from_metadata(
                    meta, self._handler, self._timeout
                )
            else:
                broker = self._brokers[id_]
                if meta.host == broker.host and meta.port == broker.port:
                    continue  # no changes
                # TODO: Can brokers update? Seems like a problem if so.
                #       Figure out and implement update/disconnect/reconnect if
                #       needed.
                raise Exception('Broker host/port change detected! %s', broker)

    def _update_topics(self, metadata):
        """Update topics with fresh metadata.

        :param metadata: Metadata for all topics
        :type metadata: Dict of `{name, metadata}` where `metadata` is
            :class:`kafka.pykafka.protocol.TopicMetadata`
        """
        # Remove old topics
        removed = set(self._topics.keys()) - set(metadata.keys())
        for name in removed:
            logger.info('Removing topic %s', self._topics[name])
            self._topics.pop(name)
        # Add/update partition information
        for name, meta in metadata.iteritems():
            if name not in self._topics:
                self._topics[name] = Topic(self._brokers, meta)
                logger.info('Adding topic %s', self._topics[name])
            else:
                self._topics[name].update(meta)

    def discover_offset_manager(self, consumer_group_name):
        # arbitrarily choose a broker, since this request can go to any
        broker = self.brokers[self.brokers.keys()[0]]
        backoff, retries = 2, 0
        MAX_RETRIES = 3
        while True:
            try:
                retries += 1
                req = ConsumerMetadataRequest(consumer_group_name)
                future = broker.handler.request(req)
                res = future.get(ConsumerMetadataResponse)
            except Exception:
                logger.debug('Error discovering offset manager. Sleeping for {}s'.format(backoff))
                if retries < MAX_RETRIES:
                    time.sleep(backoff)  # XXX - not sure if this works here
                    backoff = pow(backoff, 2)
                else:
                    raise
            else:
                coordinator = self.brokers.get(res.coordinator_id, None)
                if coordinator is None:
                    raise Exception('Coordinator broker with id {} not found'.format(res.coordinator_id))
                return coordinator

    def update(self):
        """Update known brokers and topics."""
        metadata = self._get_metadata()
        self._update_brokers(metadata.brokers)
        self._update_topics(metadata.topics)
        # N.B.: Partitions are updated as part of Topic updates.
