# -*- coding: utf-8 -*-
import asyncio
import logging
import sys
from asyncio import Event
from contextlib import suppress
from typing import Union

import aiosasl
import aioxmpp
from aiosasl import AuthenticationFailure
from aioxmpp import ibr
from aioxmpp.dispatcher import SimpleMessageDispatcher
from loguru import logger
from spade.message import Message
from spade.presence import PresenceManager
from spade_pubsub import PubSubMixin


class Artifact(PubSubMixin):
    def __init__(self, jid, password, verify_security=False):
        """
        Creates an artifact

        Args:
          jid (str): The identifier of the artifact in the form username@server
          password (str): The password to connect to the server
          verify_security (bool): Wether to verify or not the SSL certificates
        """
        self.jid = aioxmpp.JID.fromstr(jid)
        self.password = password
        self.verify_security = verify_security

        self._values = {}

        self.conn_coro = None
        self.stream = None
        self.client = None
        self.message_dispatcher = None
        self.presence = None

        self.loop = asyncio.new_event_loop()

        self.queue = asyncio.Queue(loop=self.loop)
        self._alive = Event()

    def start(self, auto_register=True):
        """
        Connects the artifact to the server, runs setup and runs the main loop.

        Args:
            auto_register (bool): register the agent in the server (Default value = True)
        """

        try:
            self.loop.run_until_complete(self._async_start(auto_register))
            self.loop.run_until_complete(self.run())
        finally:  # pragma: no cover
            if sys.version_info >= (3, 7):
                tasks = asyncio.all_tasks(loop=self.loop)  # pragma: no cover
            else:
                tasks = asyncio.Task.all_tasks(loop=self.loop)  # pragma: no cover
            for task in tasks:
                task.cancel()
                with suppress(asyncio.CancelledError):
                    self.loop.run_until_complete(task)
            self.loop.close()

    async def _async_start(self, auto_register=True):
        """
        Starts the agent from a coroutine. This fires some actions:

            * if auto_register: register the agent in the server
            * runs the event loop
            * connects the agent to the server
            * runs the registered behaviours

        Args:
          auto_register (bool, optional): register the agent in the server (Default value = True)

        """

        await self._hook_plugin_before_connection()

        if auto_register:
            await self._async_register()
        self.client = aioxmpp.PresenceManagedClient(
            self.jid,
            aioxmpp.make_security_layer(
                self.password, no_verify=not self.verify_security
            ),
            loop=self.loop,
            logger=logging.getLogger(self.jid.localpart),
        )

        # obtain an instance of the service
        self.message_dispatcher = self.client.summon(SimpleMessageDispatcher)

        # Presence service
        self.presence = PresenceManager(self)

        await self._async_connect()

        # register a message callback here
        self.message_dispatcher.register_callback(
            aioxmpp.MessageType.CHAT,
            None,
            self._message_received,
        )

        await self._hook_plugin_after_connection()

        await self.setup()
        self._alive.set()

    async def _hook_plugin_before_connection(self):
        """
        Overload this method to hook a plugin before connetion is done
        """
        pass

    async def _hook_plugin_after_connection(self):
        """
        Overload this method to hook a plugin after connetion is done
        """
        pass

    async def _async_connect(self):  # pragma: no cover
        """ connect and authenticate to the XMPP server. Async mode. """
        try:
            self.conn_coro = self.client.connected()
            aenter = type(self.conn_coro).__aenter__(self.conn_coro)
            self.stream = await aenter
            logger.info(f"Artifact {str(self.jid)} connected and authenticated.")
        except aiosasl.AuthenticationFailure:
            raise AuthenticationFailure(
                "Could not authenticate the artifact. Check user and password or use auto_register=True"
            )

    async def _async_register(self):  # pragma: no cover
        """ Register the artifact in the XMPP server from a coroutine. """
        metadata = aioxmpp.make_security_layer(None, no_verify=not self.verify_security)
        query = ibr.Query(self.jid.localpart, self.password)
        _, stream, features = await aioxmpp.node.connect_xmlstream(
            self.jid, metadata, loop=self.loop
        )
        await ibr.register(stream, query)

    async def setup(self):
        """
        Setup artifact before startup.
        This coroutine may be overloaded.
        """
        await asyncio.sleep(0)

    async def run(self):
        """
        Main body of the artifact.
        This coroutine SHOULD be overloaded.
        """
        raise NotImplementedError

    @property
    def name(self):
        """ Returns the name of the artifact (the string before the '@') """
        return self.jid.localpart

    def stop(self):
        """
        Stop the artifact
        """
        return self.loop.run_until_complete(self._async_stop())

    async def _async_stop(self):
        """ Stops an artifact and kills all its behaviours. """
        if self.presence:
            self.presence.set_unavailable()

        """ Discconnect from XMPP server. """
        if self.is_alive():
            # Disconnect from XMPP server
            self.client.stop()
            aexit = self.conn_coro.__aexit__(*sys.exc_info())
            await aexit
            logger.info("Client disconnected.")

        self._alive.clear()

    def is_alive(self):
        """
        Checks if the artifact is alive.

        Returns:
          bool: wheter the artifact is alive or not

        """
        return self._alive.is_set()

    def set(self, name, value):
        """
        Stores a knowledge item in the artifact knowledge base.

        Args:
          name (str): name of the item
          value (object): value of the item

        """
        self._values[name] = value

    def get(self, name):
        """
        Recovers a knowledge item from the artifact's knowledge base.

        Args:
          name(str): name of the item

        Returns:
          object: the object retrieved or None

        """
        if name in self._values:
            return self._values[name]
        else:
            return None

    def _message_received(self, msg):
        """
        Callback run when an XMPP Message is reveived.
        The aioxmpp.Message is converted to spade.message.Message

        Args:
          msg (aioxmpp.Messagge): the message just received.

        Returns:
            list(asyncio.Future): a list of futures of the append of the message at each matched behaviour.

        """

        msg = Message.from_node(msg)
        logger.debug(f"Got message: {msg}")
        self.loop.run_until_complete(self.queue.put(msg))

    async def send(self, msg: Message):
        """
        Sends a message.

        Args:
            msg (spade.message.Message): the message to be sent.
        """
        if not msg.sender:
            msg.sender = str(self.jid)
            logger.debug(f"Adding artifact's jid as sender to message: {msg}")
        aioxmpp_msg = msg.prepare()
        await self.client.send(aioxmpp_msg)
        msg.sent = True

    async def receive(self, timeout: float = None) -> Union[Message, None]:
        """
        Receives a message for this artifact.
        If timeout is not None it returns the message or "None"
        after timeout is done.

        Args:
            timeout (float): number of seconds until return

        Returns:
            spade.message.Message: a Message or None
        """
        if timeout:
            coro = self.queue.get()
            try:
                msg = await asyncio.wait_for(coro, timeout=timeout)
            except asyncio.TimeoutError:
                msg = None
        else:
            try:
                msg = self.queue.get_nowait()
            except asyncio.QueueEmpty:
                msg = None
        return msg

    def mailbox_size(self) -> int:
        """
        Checks if there is a message in the mailbox

        Returns:
          int: the number of messages in the mailbox

        """
        return self.queue.qsize()