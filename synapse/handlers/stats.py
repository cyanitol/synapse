# -*- coding: utf-8 -*-
# Copyright 2018 New Vector Ltd
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import logging

from twisted.internet import defer

from synapse.api.constants import EventTypes, JoinRules, Membership
from synapse.handlers.state_deltas import StateDeltasHandler
from synapse.metrics import event_processing_positions
from synapse.metrics.background_process_metrics import run_as_background_process
from synapse.types import UserID
from synapse.util.metrics import Measure

logger = logging.getLogger(__name__)


class StatsHandler(StateDeltasHandler):
    """Handles keeping the *_stats tables updated with a simple time-series of
    information about the users, rooms and media on the server, such that admins
    have some idea of who is consuming their resources.

    Heavily derived from UserDirectoryHandler
    """

    def __init__(self, hs):
        super(StatsHandler, self).__init__(hs)
        self.hs = hs
        self.store = hs.get_datastore()
        self.state = hs.get_state_handler()
        self.server_name = hs.hostname
        self.clock = hs.get_clock()
        self.notifier = hs.get_notifier()
        self.is_mine_id = hs.is_mine_id
        self.stats_bucket_size = hs.config.stats_bucket_size

        # The current position in the current_state_delta stream
        self.pos = None

        if hs.config.stats_enabled:
            self.notifier.add_replication_callback(self.notify_new_event)

            # We kick this off so that we don't have to wait for a change before
            # we start populating stats
            self.clock.call_later(0, self.notify_new_event)

    def notify_new_event(self):
        """Called when there may be more deltas to process
        """
        if not self.hs.config.stats_enabled:
            return

        lock = self.store.stats_delta_processing_lock

        @defer.inlineCallbacks
        def process():
            try:
                yield self._unsafe_process()
            finally:
                lock.release()

        if lock.acquire(blocking=False):
            # we only want to run this process one-at-a-time,
            # and also, if the initial background updater wants us to keep out,
            # we should respect that.
            try:
                run_as_background_process("stats.notify_new_event", process)
            except:  # noqa: E722 – re-raised so fine
                lock.release()
                raise

    @defer.inlineCallbacks
    def _unsafe_process(self):
        # If self.pos is None then means we haven't fetched it from DB
        if self.pos is None or None in self.pos.values():
            self.pos = yield self.store.get_stats_positions()

        # If still None then the initial background update hasn't started yet
        if self.pos is None or None in self.pos.values():
            return None

        # Loop round handling deltas until we're up to date
        with Measure(self.clock, "stats_delta"):
            while True:
                deltas = yield self.store.get_current_state_deltas(
                    self.pos["state_delta_stream_id"]
                )
                if not deltas:
                    break

                logger.debug("Handling %d state deltas", len(deltas))
                yield self._handle_deltas(deltas)

                self.pos["state_delta_stream_id"] = deltas[-1]["stream_id"]

                event_processing_positions.labels("stats").set(
                    self.pos["state_delta_stream_id"]
                )

                if self.pos is not None:
                    yield self.store.update_stats_positions(self.pos)

    @defer.inlineCallbacks
    def _handle_deltas(self, deltas):
        """
        Called with the state deltas to process
        """
        for delta in deltas:
            typ = delta["type"]
            state_key = delta["state_key"]
            room_id = delta["room_id"]
            event_id = delta["event_id"]
            stream_id = delta["stream_id"]
            prev_event_id = delta["prev_event_id"]
            stream_pos = delta["stream_id"]

            logger.debug("Handling: %r %r, %s", typ, state_key, event_id)

            token = yield self.store.get_earliest_token_for_stats("room", room_id)

            # If the earliest token to begin from is larger than our current
            # stream ID, skip processing this delta.
            if token is not None and token >= stream_id:
                logger.debug(
                    "Ignoring: %s as earlier than this room's initial ingestion event",
                    event_id,
                )
                continue

            if event_id is None and prev_event_id is None:
                # Errr...
                continue

            event_content = {}

            if event_id is not None:
                event = yield self.store.get_event(event_id, allow_none=True)
                if event:
                    event_content = event.content or {}

            # We use stream_pos here rather than fetch by event_id as event_id
            # may be None
            now = yield self.store.get_received_ts_by_stream_pos(stream_pos)
            now = int(now) // 1000

            room_stats_delta = {}
            room_stats_complete = False

            if prev_event_id is None:
                # this state event doesn't overwrite another,
                # so it is a new effective/current state event
                room_stats_delta["current_state_events"] = (
                    room_stats_delta.get("current_state_events", 0) + 1
                )

            if typ == EventTypes.Member:
                # we could use _get_key_change here but it's a bit inefficient
                # given we're not testing for a specific result; might as well
                # just grab the prev_membership and membership strings and
                # compare them.
                # We take None rather than leave as a previous membership
                # in the absence of a previous event because we do not want to
                # reduce the leave count when a new-to-the-room user joins.
                prev_membership = None
                if prev_event_id is not None:
                    prev_event = yield self.store.get_event(
                        prev_event_id, allow_none=True
                    )
                    if prev_event:
                        prev_event_content = prev_event.content
                        prev_membership = prev_event_content.get(
                            "membership", Membership.LEAVE
                        )

                membership = event_content.get("membership", Membership.LEAVE)

                if prev_membership is None:
                    logger.debug("No previous membership for this user.")
                elif prev_membership == Membership.JOIN:
                    room_stats_delta["joined_members"] = (
                        room_stats_delta.get("joined_members", 0) - 1
                    )
                elif prev_membership == Membership.INVITE:
                    room_stats_delta["invited_members"] = (
                        room_stats_delta.get("invited_members", 0) - 1
                    )
                elif prev_membership == Membership.LEAVE:
                    room_stats_delta["left_members"] = (
                        room_stats_delta.get("left_members", 0) - 1
                    )
                elif prev_membership == Membership.BAN:
                    room_stats_delta["banned_members"] = (
                        room_stats_delta.get("banned_members", 0) - 1
                    )
                else:
                    err = "%s is not a valid prev_membership" % (repr(prev_membership),)
                    logger.error(err)
                    raise ValueError(err)

                if membership == Membership.JOIN:
                    room_stats_delta["joined_members"] = (
                        room_stats_delta.get("joined_members", 0) + 1
                    )
                elif membership == Membership.INVITE:
                    room_stats_delta["invited_members"] = (
                        room_stats_delta.get("invited_members", 0) + 1
                    )
                elif membership == Membership.LEAVE:
                    room_stats_delta["left_members"] = (
                        room_stats_delta.get("left_members", 0) + 1
                    )
                elif membership == Membership.BAN:
                    room_stats_delta["banned_members"] = (
                        room_stats_delta.get("banned_members", 0) + 1
                    )
                else:
                    err = "%s is not a valid membership" % (repr(membership),)
                    logger.error(err)
                    raise ValueError(err)

                user_id = state_key
                if self.is_mine_id(user_id) and membership in (
                    Membership.JOIN,
                    Membership.LEAVE,
                ):
                    # update user_stats as it's one of our users
                    public = yield self._is_public_room(room_id)

                    field = "public_rooms" if public else "private_rooms"
                    delta = +1 if membership == Membership.JOIN else -1

                    yield self.store.update_stats_delta(
                        now, "user", user_id, {field: delta}
                    )

            elif typ == EventTypes.Create:
                # Newly created room. Add it with all blank portions.
                yield self.store.update_room_state(
                    room_id,
                    {
                        "join_rules": None,
                        "history_visibility": None,
                        "encryption": None,
                        "name": None,
                        "topic": None,
                        "avatar": None,
                        "canonical_alias": None,
                    },
                )

                room_stats_complete = True

            elif typ == EventTypes.JoinRules:
                old_room_state = yield self.store.get_room_state(room_id)
                yield self.store.update_room_state(
                    room_id, {"join_rules": event_content.get("join_rule")}
                )

                # whether the room would be public anyway,
                # because of history_visibility
                other_field_gives_publicity = (
                    old_room_state["history_visibility"] == "world_readable"
                )

                if not other_field_gives_publicity:
                    is_public = yield self._get_key_change(
                        prev_event_id, event_id, "join_rule", JoinRules.PUBLIC
                    )
                    if is_public is not None:
                        yield self.update_public_room_stats(now, room_id, is_public)

            elif typ == EventTypes.RoomHistoryVisibility:
                old_room_state = yield self.store.get_room_state(room_id)
                yield self.store.update_room_state(
                    room_id,
                    {"history_visibility": event_content.get("history_visibility")},
                )

                # whether the room would be public anyway,
                # because of join_rule
                other_field_gives_publicity = (
                    old_room_state["join_rules"] == JoinRules.PUBLIC
                )

                if not other_field_gives_publicity:
                    is_public = yield self._get_key_change(
                        prev_event_id, event_id, "history_visibility", "world_readable"
                    )
                    if is_public is not None:
                        yield self.update_public_room_stats(now, room_id, is_public)

            elif typ == EventTypes.Encryption:
                yield self.store.update_room_state(
                    room_id, {"encryption": event_content.get("algorithm")}
                )
            elif typ == EventTypes.Name:
                yield self.store.update_room_state(
                    room_id, {"name": event_content.get("name")}
                )
            elif typ == EventTypes.Topic:
                yield self.store.update_room_state(
                    room_id, {"topic": event_content.get("topic")}
                )
            elif typ == EventTypes.RoomAvatar:
                yield self.store.update_room_state(
                    room_id, {"avatar": event_content.get("url")}
                )
            elif typ == EventTypes.CanonicalAlias:
                yield self.store.update_room_state(
                    room_id, {"canonical_alias": event_content.get("alias")}
                )

            if room_stats_complete:
                yield self.store.update_stats_delta(
                    now,
                    "room",
                    room_id,
                    room_stats_delta,
                    complete_with_stream_id=stream_id,
                )

            elif len(room_stats_delta) > 0:
                yield self.store.update_stats_delta(
                    now, "room", room_id, room_stats_delta
                )

    @defer.inlineCallbacks
    def update_public_room_stats(self, ts, room_id, is_public):
        """
        Increment/decrement a user's number of public rooms when a room they are
        in changes to/from public visibility.

        Args:
            ts (int): Timestamp in seconds
            room_id (str)
            is_public (bool)
        """
        # For now, blindly iterate over all local users in the room so that
        # we can handle the whole problem of copying buckets over as needed
        user_ids = yield self.store.get_users_in_room(room_id)

        for user_id in user_ids:
            if self.hs.is_mine(UserID.from_string(user_id)):
                yield self.store.update_stats_delta(
                    ts,
                    "user",
                    user_id,
                    {
                        "public_rooms": +1 if is_public else -1,
                        "private_rooms": -1 if is_public else +1,
                    },
                )

    @defer.inlineCallbacks
    def _is_public_room(self, room_id):
        join_rules = yield self.state.get_current_state(room_id, EventTypes.JoinRules)
        history_visibility = yield self.state.get_current_state(
            room_id, EventTypes.RoomHistoryVisibility
        )

        if (join_rules and join_rules.content.get("join_rule") == JoinRules.PUBLIC) or (
            (
                history_visibility
                and history_visibility.content.get("history_visibility")
                == "world_readable"
            )
        ):
            return True
        else:
            return False
