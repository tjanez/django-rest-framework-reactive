import redis
import redis.connection
import cPickle as pickle
import logging
import traceback
import json

from django import db
from django.core import exceptions
from django.core.management import base

from genesis.utils.formatters import BraceMessage as __
from genesis.queryobserver import connection
from genesis.queryobserver.pool import pool

# Logger.
logger = logging.getLogger(__name__)


class RedisObserverEventHandler(object):
    """
    Query observer handler that receives events via Redis.
    """

    def __call__(self):
        """
        Entry point.
        """

        # Establish a connection with Redis server.
        self._redis = redis.StrictRedis(**connection.get_redis_settings())
        self._pubsub = self._redis.pubsub(ignore_subscribe_messages=True)
        self._pubsub.subscribe(connection.QUERYOBSERVER_REDIS_CHANNEL)

        for event in self._pubsub.listen():
            # Events are assumed to be pickled data.
            try:
                event = pickle.loads(event['data'])
            except ValueError:
                logger.error(__("Ignoring received malformed event '{}'.", event['data'][:20]))
                continue

            # Handle event.
            try:
                event_name = event.pop('event')
                handler = getattr(self, 'event_%s' % event_name)
            except AttributeError:
                logger.error(__("Ignoring unimplemented event '{}'.", event_name))
                continue
            except KeyError:
                logger.error(__("Ignoring received malformed event '{}'.", event))
                continue

            try:
                handler(**event)
            except:
                logger.error(__("Unhandled exception while executing event '{}'.", event_name))
                logger.error(traceback.format_exc())
            finally:
                db.close_old_connections()

    def event_table_insert(self, table):
        pool.notify_update(table)

    def event_table_update(self, table):
        pool.notify_update(table)

    def event_table_remove(self, table):
        pool.notify_update(table)

    def event_subscriber_gone(self, subscriber):
        pool.remove_subscriber(subscriber)


class WSGIObserverCommandHandler(object):
    """
    A WSGI-based RPC server for the query observer API.
    """

    def __init__(self, database):
        """
        Constructs a new WSGI server for handling query observer RPC.

        :param database: Database configuration to use for queries
        """

        self.database = database

    def __call__(self, environ, start_response):
        """
        Handles an incoming RPC request.
        """

        try:
            request = pickle.loads(environ['wsgi.input'].read())
            if not isinstance(request, dict):
                raise ValueError

            command = request.pop('command')
            handler = getattr(self, 'command_%s' % command)
        except (KeyError, ValueError, AttributeError, EOFError):
            start_response('400 Bad Request', [('Content-Type', 'text/json')])
            return [json.dumps({'error': "Bad request."})]

        try:
            response = handler(**request)
            start_response('200 OK', [('Content-Type', 'text/json')])
            return [json.dumps(response)]
        except TypeError:
            start_response('400 Bad Request', [('Content-Type', 'text/json')])
            return [json.dumps({'error': "Bad request."})]
        except:
            logger.error(__("Unhandled exception while executing command '{}'.", command))
            logger.error(traceback.format_exc())

            start_response('500 Internal Server Error', [('Content-Type', 'text/json')])
            return [json.dumps({'error': "Internal server error."})]
        finally:
            db.close_old_connections()

    def _get_queryset(self, query):
        """
        Returns a queryset given a query.
        """

        # Create a queryset back from the pickled query.
        queryset = query.model.objects.db_manager(self.database).all()
        queryset.query = query
        return queryset

    def command_create_observer(self, query, subscriber):
        """
        Starts observing a specific query.

        :param query: Query instance to observe
        :param subscriber: Subscriber channel name
        :return: Serialized current query results
        """

        observer = pool.observe_queryset(self._get_queryset(query), subscriber)
        return {
            'observer': observer.id,
            'items': observer.evaluate(),
        }

    def command_unsubscribe_observer(self, observer, subscriber):
        """
        Unsubscribes a specific subscriber from an observer.

        :param observer: Query observer identifier
        :param subscriber: Subscriber channel name
        """

        pool.unobserve_queryset(observer, subscriber)