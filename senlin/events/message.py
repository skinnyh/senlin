# Licensed under the Apache License, Version 2.0 (the "License"); you may
# not use this file except in compliance with the License. You may obtain
# a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
# WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
# License for the specific language governing permissions and limitations
# under the License.

from oslo_config import cfg
from oslo_utils import reflection

from senlin.common import utils
from senlin.objects import notification as nobj


class MessageEvent(object):
    """Message driver for event dumping"""

    @staticmethod
    def _check_entity(e):
        e_type = reflection.get_class_name(e, fully_qualified=False)
        return e_type.upper()

    @staticmethod
    def _get_action_name(action):
        name = action.split('_', 1)[1]
        return name.lower()

    @classmethod
    def _notify_cluster_action(cls, ctx, level, cluster, action, **kwargs):
        action_name = cls._get_action_name(action.action)
        priority = utils.level_from_number(level).lower()
        publisher = nobj.NotificationPublisher(
            host=cfg.CONF.host, binary='senlin-engine')
        publisher.obj_set_defaults()
        phase = kwargs.get('phase')
        event_type = nobj.EventType(
            object='cluster', action=action_name, phase=phase)
        payload = nobj.ClusterActionPayload(cluster, action)
        notification = nobj.ClusterActionNotification(
            context=ctx, priority=priority, publisher=publisher,
            event_type=event_type, payload=payload)
        notification.emit(ctx)

    @classmethod
    def _notify_node_action(cls, ctx, level, node, action, **kwargs):
        action_name = cls._get_action_name(action.action)
        priority = utils.level_from_number(level).lower()
        publisher = nobj.NotificationPublisher(
            host=cfg.CONF.host, binary='senlin-engine')
        publisher.obj_set_defaults()
        phase = kwargs.get('phase')
        event_type = nobj.EventType(
            object='node', action=action_name, phase=phase)
        payload = nobj.NodeActionPayload(node, action)
        notification = nobj.NodeActionNotification(
            context=ctx, priority=priority, publisher=publisher,
            event_type=event_type, payload=payload)
        notification.emit(ctx)

    @classmethod
    def dump(cls, ctx, level, entity, action, **kwargs):
        """Dump the provided event into message queue.

        :param ctx: The request context.
        :param level: An integer as defined by python logging module.
        :param entity: A cluster or a node object.
        :param action: An action object for the current operation.
        :param dict kwargs: Other keyword arguments for the operation.
        """
        # TODO(Qiming): Add filter about levels that should not be logged
        etype = cls._check_entity(entity)
        if etype == 'CLUSTER':
            cls._notify_cluster_action(ctx, level, entity, action, **kwargs)
        else:
            cls._notify_node_action(ctx, level, entity, action, **kwargs)