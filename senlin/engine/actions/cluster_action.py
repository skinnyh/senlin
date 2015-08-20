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

import random

from oslo_log import log as logging
from oslo_utils import timeutils

from senlin.common import consts
from senlin.common import exception
from senlin.common.i18n import _
from senlin.common.i18n import _LE
from senlin.common import scaleutils
from senlin.db import api as db_api
from senlin.engine.actions import base
from senlin.engine import cluster as cluster_mod
from senlin.engine import cluster_policy as cp_mod
from senlin.engine import dispatcher
from senlin.engine import node as node_mod
from senlin.engine import scheduler
from senlin.engine import senlin_lock
from senlin.policies import base as policy_mod

LOG = logging.getLogger(__name__)


class ClusterAction(base.Action):
    '''An action performed on a cluster.'''

    ACTIONS = (
        CLUSTER_CREATE, CLUSTER_DELETE, CLUSTER_UPDATE,
        CLUSTER_ADD_NODES, CLUSTER_DEL_NODES,
        CLUSTER_RESIZE,
        CLUSTER_SCALE_OUT, CLUSTER_SCALE_IN,
        CLUSTER_ATTACH_POLICY, CLUSTER_DETACH_POLICY, CLUSTER_UPDATE_POLICY
    ) = (
        consts.CLUSTER_CREATE, consts.CLUSTER_DELETE, consts.CLUSTER_UPDATE,
        consts.CLUSTER_ADD_NODES, consts.CLUSTER_DEL_NODES,
        consts.CLUSTER_RESIZE,
        consts.CLUSTER_SCALE_OUT, consts.CLUSTER_SCALE_IN,
        consts.CLUSTER_ATTACH_POLICY, consts.CLUSTER_DETACH_POLICY,
        consts.CLUSTER_UPDATE_POLICY,
    )

    def __init__(self, context, action, **kwargs):
        super(ClusterAction, self).__init__(context, action, **kwargs)

    def _wait_for_dependents(self):
        self.get_status()
        reason = ''
        while self.status != self.READY:
            if self.status == self.FAILED:
                reason = _('%(action)s [%(id)s] failed due to dependent '
                           'action failure') % {'action': self.action,
                                                'id': self.id}
                LOG.debug(reason)
                return self.RES_ERROR, reason

            if self.is_cancelled():
                # During this period, if cancel request come, cancel this
                # cluster operation immediately, then release the cluster
                # lock and return.
                reason = _('%(action)s %(id)s cancelled') % {
                    'action': self.action, 'id': self.id}
                LOG.debug(reason)
                return self.RES_CANCEL, reason

            if self.is_timeout():
                # Action timeout, return
                reason = _('%(action)s %(id)s timeout') % {
                    'action': self.action, 'id': self.id}
                LOG.debug(reason)
                return self.RES_TIMEOUT

            # Continue waiting (with reschedule)
            scheduler.reschedule(self, 1)
            self.get_status()

        return self.RES_OK, 'All dependents ended with success'

    def _create_nodes(self, cluster, count):
        '''Utility method for node creation.'''
        placement = self.data.get('placement', None)

        nodes = []
        for m in range(count):
            db_cluster = db_api.cluster_get(self.context, cluster.id)
            index = db_cluster.next_index

            kwargs = {
                'user': cluster.user,
                'project': cluster.project,
                'domain': cluster.domain,
                'index': index,
                'metadata': {}
            }
            name = 'node-%s-%003d' % (cluster.id[:8], index)
            node = node_mod.Node(name, cluster.profile_id, cluster.id,
                                 context=self.context, **kwargs)

            if placement is not None:
                # We assume placement is a list
                node.data['placement'] = placement[m]
            node.store(self.context)
            nodes.append(node.id)

            kwargs = {
                'name': 'node_create_%s' % node.id[:8],
                'target': node.id,
                'cause': base.CAUSE_DERIVED,
            }

            action = base.Action(self.context, 'NODE_CREATE', **kwargs)
            action.store(self.context)

            # Build dependency and make the new action ready
            db_api.action_add_dependency(self.context, action.id, self.id)
            action.set_status(self.READY)

            dispatcher.start_action(self.context, action_id=action.id)

        if count > 0:
            # Wait for cluster creation to complete
            res, reason = self._wait_for_dependents()
            if res == self.RES_OK:
                self.data['nodes'] = nodes
            return res, reason

        return self.RES_OK, ''

    def do_create(self, cluster):
        res = cluster.do_create(self.context)

        if not res:
            reason = 'Cluster creation failed.'
            cluster.set_status(self.context, cluster.ERROR, reason)
            return self.RES_ERROR, reason

        result, reason = self._create_nodes(cluster, cluster.desired_capacity)

        if result == self.RES_OK:
            reason = 'Cluster creation succeeded'
            cluster.set_status(self.context, cluster.ACTIVE, reason)
        elif result in [self.RES_CANCEL, self.RES_TIMEOUT, self.RES_ERROR]:
            cluster.set_status(self.context, cluster.ERROR, reason)
        else:
            # RETRY or FAILED?
            pass

        return result, reason

    def do_update(self, cluster):
        profile_id = self.inputs.get('new_profile_id')

        node_list = cluster.get_nodes()
        for node in node_list:
            kwargs = {
                'name': 'node_update_%s' % node.id[:8],
                'target': node.id,
                'cause': base.CAUSE_DERIVED,
                'inputs': {
                    'new_profile_id': profile_id,
                }
            }
            action = base.Action(self.context, 'NODE_UPDATE', **kwargs)
            action.store(self.context)

            db_api.action_add_dependency(self.context, action.id, self.id)
            action.set_status(self.READY)
            dispatcher.start_action(self.context, action_id=action.id)

        # Wait for nodes to complete update
        result = self.RES_OK
        reason = 'Update completed'
        if len(node_list) > 0:
            result, reason = self._wait_for_dependents()

        if result != self.RES_OK:
            return result, reason

        # TODO(anyone): this seems an overhead
        cluster.profile_id = profile_id
        cluster.store(self.context)

        cluster.set_status(self.context, cluster.ACTIVE, reason)
        return self.RES_OK, _('Cluster update succeeded')

    def _delete_nodes(self, cluster, nodes):
        action_name = consts.NODE_DELETE

        pd = self.data.get('deletion', None)
        if pd is not None:
            destroy = pd.get('destroy_after_delete', True)
            if not destroy:
                action_name = consts.NODE_LEAVE

        for node_id in nodes:
            action = base.Action(self.context, action_name,
                                 name='node_delete_%s' % node_id[:8],
                                 target=node_id,
                                 cause=base.CAUSE_DERIVED)
            action.store(self.context)

            # Build dependency and make the new action ready
            db_api.action_add_dependency(self.context, action.id, self.id)
            action.set_status(self.READY)

            dispatcher.start_action(self.context, action_id=action.id)

        if len(nodes) > 0:
            res, reason = self._wait_for_dependents()
            if res == self.RES_OK:
                self.data['nodes'] = nodes
            return res, reason

        return self.RES_OK, ''

    def do_delete(self, cluster):
        reason = 'Deletion in progress'
        cluster.set_status(self.context, cluster.DELETING, reason)
        nodes = [node.id for node in cluster.get_nodes()]

        # For cluster delete, we delete the nodes
        data = {
            'deletion': {
                'destroy_after_delete': True
            }
        }
        self.data.update(data)
        result, reason = self._delete_nodes(cluster, nodes)

        if result == self.RES_OK:
            res = cluster.do_delete(self.context)
            if not res:
                return self.RES_ERROR, 'Cannot delete cluster object.'
        elif result == self.RES_CANCEL:
            cluster.set_status(self.context, cluster.ACTIVE, reason)
        elif result in [self.RES_TIMEOUT, self.RES_ERROR]:
            cluster.set_status(self.context, cluster.WARNING, reason)
        else:
            # RETRY
            pass

        return result, reason

    def do_add_nodes(self, cluster):
        nodes = self.inputs.get('nodes')

        # NOTE: node states might have changed before we lock the cluster
        failures = {}
        for node_id in nodes:
            try:
                node = node_mod.Node.load(self.context, node_id)
            except exception.NodeNotFound:
                failures[node_id] = 'Node not found'
                continue

            if node.cluster_id == cluster.id:
                nodes.remove(node_id)
                continue

            if node.cluster_id is not None:
                failures[node_id] = _('Node already owned by cluster '
                                      '%s') % node.cluster_id
                continue
            if node.status != node_mod.Node.ACTIVE:
                failures[node_id] = _('Node not in ACTIVE status')
                continue

        if len(failures) > 0:
            return self.RES_ERROR, str(failures)

        reason = 'Completed adding nodes'
        if len(nodes) == 0:
            return self.RES_OK, reason

        for node_id in nodes:
            action = base.Action(self.context, 'NODE_JOIN',
                                 name='node_join_%s' % node.id[:8],
                                 target=node.id,
                                 cause=base.CAUSE_DERIVED,
                                 inputs={'cluster_id': cluster.id})
            action.store(self.context)
            db_api.action_add_dependency(self.context, action.id, self.id)
            action.set_status(self.READY)
            dispatcher.start_action(self.context, action_id=action.id)

        # Wait for dependent action if any
        result, new_reason = self._wait_for_dependents()
        if result != self.RES_OK:
            reason = new_reason
        else:
            self.data['nodes'] = nodes

        return result, reason

    def do_del_nodes(self, cluster):
        nodes = self.inputs.get('nodes')

        # NOTE: node states might have changed before we lock the cluster
        failures = {}
        for node_id in nodes:
            try:
                node = node_mod.Node.load(self.context, node_id)
            except exception.NodeNotFound:
                failures[node_id] = 'Node not found'
                continue

            if node.cluster_id is None:
                nodes.remove(node_id)

        if len(failures) > 0:
            return self.RES_ERROR, str(failures)

        reason = 'Completed deleting nodes'
        if len(nodes) == 0:
            return self.RES_OK, reason

        # For deleting nodes from cluster, set destroy to false
        data = {
            'deletion': {
                'destroy_after_delete': False,
            }
        }
        self.data.update(data)
        result, new_reason = self._delete_nodes(cluster, nodes)

        if result != self.RES_OK:
            reason = new_reason
        return result, reason

    def _check_size_params(self, cluster, desired, min_size, max_size, strict):
        '''Validate provided arguments with cluster properties.

        Sanity Checking 1: the desired, min_size, max_size parameters must
                           form a reasonable relationship among themselves,
                           if specified.
        Sanity Checking 2: the desired_capacity must be within the existing
                           range of the cluster, if new range is not provided.
        '''
        if desired is not None and strict is True:
            # recalculate/validate desired based on strict setting
            if (min_size is not None and desired < min_size):
                v = {'d': desired, 'm': min_size}
                reason = _("The target capacity (%(d)s) is less than "
                           "the specified min_size (%(m)s).") % v
                return self.RES_ERROR, reason

            if min_size is None and desired < cluster.min_size:
                v = {'d': desired, 'm': cluster.min_size}
                reason = _("The target capacity (%(d)s) is less than "
                           "the cluster's min_size (%(m)s).") % v
                return self.RES_ERROR, reason

            if (max_size is not None and desired > max_size and
                    max_size >= 0):
                v = {'d': desired, 'm': max_size}
                reason = _("The target capacity (%(d)s) is greater "
                           "than the specified max_size (%(m)s).") % v
                return self.RES_ERROR, reason

            if (max_size is None and
                    desired > cluster.max_size and cluster.max_size >= 0):
                v = {'d': desired, 'm': cluster.max_size}
                reason = _("The target capacity (%(d)s) is greater "
                           "than the cluster's max_size (%(m)s).") % v
                return self.RES_ERROR, reason

        if min_size is not None:
            if max_size is not None and max_size >= 0 and min_size > max_size:
                reason = _("The specified min_size is greater than the "
                           "specified max_size.")
                return self.RES_ERROR, reason

            if (max_size is None and cluster.max_size >= 0 and
                    min_size > cluster.max_size):
                reason = _("The specified min_size is greater than the "
                           "current max_size of the cluster.")
                return self.RES_ERROR, reason

            if desired is None and min_size > cluster.desired_capacity:
                reason = _("The specified min_size is greater than the "
                           "current desired_capacity of the cluster.")
                return self.RES_ERROR, reason

        if max_size is not None:
            if (min_size is None and max_size >= 0
                    and max_size < cluster.min_size):
                reason = _("The specified max_size is less than the "
                           "current min_size of the cluster.")
                return self.RES_ERROR, reason
            if desired is None and max_size < cluster.desired_capacity:
                reason = _("The specified max_size is less than the "
                           "current desired_capacity of the cluster.")
                return self.RES_ERROR, reason

        return self.RES_OK, ''

    def _update_cluster_properties(self, cluster, desired, min_size, max_size):
        # update cluster properties related to size and profile
        need_store = False
        if min_size is not None and min_size != cluster.min_size:
            cluster.min_size = min_size
            need_store = True
        if max_size is not None and max_size != cluster.max_size:
            cluster.max_size = max_size
            need_store = True
        if desired is not None and desired != cluster.desired_capacity:
            cluster.desired_capacity = desired
            need_store = True

        if need_store is False:
            # ensure node list is up to date
            cluster._load_runtime_data(self.context)
            return self.RES_OK, ''

        cluster.updated_time = timeutils.utcnow()
        cluster.status_reason = _('Cluster properties updated.')
        res = cluster.store(self.context)
        if not res:
            reason = _('Cluster object cannot be updated.')
            # Reset status to active
            cluster.set_status(self.context, cluster.ACTIVE, reason)
            return self.RES_ERROR, reason

        return self.RES_OK, ''

    def do_resize(self, cluster):
        adj_type = self.inputs.get(consts.ADJUSTMENT_TYPE)
        number = self.inputs.get(consts.ADJUSTMENT_NUMBER)
        min_size = self.inputs.get(consts.ADJUSTMENT_MIN_SIZE)
        max_size = self.inputs.get(consts.ADJUSTMENT_MAX_SIZE)
        min_step = self.inputs.get(consts.ADJUSTMENT_MIN_STEP)
        strict = self.inputs.get(consts.ADJUSTMENT_STRICT, False)

        desired = cluster.desired_capacity
        if adj_type is not None:
            # number must be not None according to previous tests
            desired = scaleutils.calculate_desired(
                cluster.desired_capacity, adj_type, number, min_step)

        # truncate adjustment if permitted (strict==False)
        if strict is False:
            desired = scaleutils.truncate_desired(
                cluster, desired, min_size, max_size)

        # check provided params against current properties
        # desired is checked when strict is True
        result, reason = self._check_size_params(cluster, desired, min_size,
                                                 max_size, strict)
        if result != self.RES_OK:
            return result, reason

        # save sanitized properties
        result, reason = self._update_cluster_properties(
            cluster, desired, min_size, max_size)
        if result != self.RES_OK:
            return result, reason

        node_list = cluster.get_nodes()
        current_size = len(node_list)
        desired = cluster.desired_capacity

        # delete nodes if necessary
        if desired < current_size:
            adjustment = current_size - desired
            if 'deletion' not in self.data:
                self.data['deletion'] = {'count': adjustment}
            candidates = []
            # Choose victims randomly
            i = adjustment
            while i > 0:
                r = random.randrange(len(node_list))
                candidates.append(node_list[r].id)
                node_list.remove(node_list[r])
                i = i - 1

            result, reason = self._delete_nodes(cluster, candidates)
            if result != self.RES_OK:
                return result, reason

        # Create new nodes if desired_capacity increased
        if desired > current_size:
            delta = desired - current_size
            if 'creation' not in self.data:
                self.data['creation'] = {'count': delta}
            result, reason = self._create_nodes(cluster, delta)
            if result != self.RES_OK:
                return result, reason

        cluster.set_status(self.context, cluster.ACTIVE, reason)
        return self.RES_OK, _('Cluster resize succeeded')

    def do_scale_out(self, cluster):
        # We use policy output if any, or else the count is
        # set to 1 as default.
        count = 0
        pd = self.data.get('creation', None)
        if pd is not None:
            count = pd.get('count', 1)
        else:
            # If no scaling policy is attached, use the
            # input count directly
            count = self.inputs.get('count', 1)

        if count == 0:
            return self.RES_OK, 'No scaling needed based on policy checking'

        # Update desired_capacity of cluster
        nodes = db_api.node_get_all_by_cluster(self.context, cluster.id)
        current_size = len(nodes)
        desired_capacity = current_size + count
        result, reason = self._update_cluster_properties(
            cluster, desired_capacity, None, None)
        if result != self.RES_OK:
            return result, reason

        # Create new nodes to meet desired_capacity
        result, reason = self._create_nodes(cluster, count)

        if result == self.RES_OK:
            reason = 'Cluster scaling succeeded'
            cluster.set_status(self.context, cluster.ACTIVE, reason)
        elif result in [self.RES_CANCEL, self.RES_TIMEOUT, self.RES_FAILED]:
            cluster.set_status(self.context, cluster.ERROR, reason)
        else:
            # RETRY or FAILED?
            pass

        return result, reason

    def do_scale_in(self, cluster):
        # We use policy output if any, or else the count is
        # set to 1 as default.
        pd = self.data.get('deletion', None)
        if pd is not None:
            count = pd.get('count', 1)
            # Try get candidates (set by deletion policy if attached)
            candidates = self.data.get('candidates')
            if not candidates:
                candidates = []
        else:
            # If no scaling policy is attached, use the
            # input count directly
            count = self.inputs.get('count', 1)
            candidates = []

        if count == 0:
            return self.RES_OK, 'No scaling needed based on policy checking'

        # Update desired_capacity of cluster
        nodes = db_api.node_get_all_by_cluster(self.context, cluster.id)
        current_size = len(nodes)
        desired_capacity = current_size - count
        result, reason = self._update_cluster_properties(
            cluster, desired_capacity, None, None)
        if result != self.RES_OK:
            return result, reason

        # Choose victims randomly
        if len(candidates) == 0:
            nodes = db_api.node_get_all_by_cluster(self.context, cluster.id)
            i = count
            while i > 0:
                r = random.randrange(len(nodes))
                candidates.append(nodes[r].id)
                nodes.remove(nodes[r])
                i = i - 1

        # The policy data may contain destroy flag and grace period option
        result, new_reason = self._delete_nodes(cluster, candidates)

        if result == self.RES_OK:
            reason = 'Cluster scaling succeeded'
            cluster.set_status(self.context, cluster.ACTIVE, reason)
        elif result in [self.RES_CANCEL, self.RES_TIMEOUT, self.RES_FAILED]:
            cluster.set_status(self.context, cluster.ERROR, reason)
        else:
            # RETRY or FAILED?
            pass

        return result, reason

    def do_attach_policy(self, cluster):
        '''Attach policy to the cluster.'''

        policy_id = self.inputs.get('policy_id', None)
        policy = policy_mod.Policy.load(self.context, policy_id)
        # Check if policy has already been attached
        all_policies = db_api.cluster_policy_get_all(self.context, cluster.id)
        for existing in all_policies:
            # Policy already attached
            if existing.policy_id == policy_id:
                return self.RES_OK, 'Policy already attached'

            # Detect policy type conflicts
            curr = policy_mod.Policy.load(self.context, existing.policy_id)
            if curr.type == policy.type:
                raise exception.PolicyTypeConflict(policy_type=policy.type)

        res, data = policy.attach(cluster)
        if not res:
            return self.RES_ERROR, _('Failed attaching policy.')

        # Initialize data field of cluster_policy object with information
        # generated during policy attaching
        values = {
            'priority': self.inputs['priority'],
            'cooldown': self.inputs['cooldown'],
            'level': self.inputs['level'],
            'enabled': self.inputs['enabled'],
            'data': data,
        }

        cp = cp_mod.ClusterPolicy(cluster.id, policy_id, **values)
        cp.store(self.context)

        cluster.add_policy(policy)
        return self.RES_OK, _('Policy attached.')

    def do_detach_policy(self, cluster):
        policy_id = self.inputs.get('policy_id', None)
        if not policy_id:
            raise exception.PolicyNotSpecified()

        policy = policy_mod.Policy.load(self.context, policy_id)
        res, data = policy.detach(cluster)
        if not res:
            return self.RES_ERROR, _('Failed detaching policy.')

        db_api.cluster_policy_detach(self.context, cluster.id, policy_id)

        cluster.remove_policy(policy)
        return self.RES_OK, _('Policy detached.')

    def do_update_policy(self, cluster):
        policy_id = self.inputs.get('policy_id', None)
        if not policy_id:
            raise exception.PolicyNotSpecified()

        values = {}
        cooldown = self.inputs.get('cooldown')
        if cooldown is not None:
            values['cooldown'] = cooldown
        level = self.inputs.get('level')
        if level is not None:
            values['level'] = level
        priority = self.inputs.get('priority')
        if priority is not None:
            values['priority'] = priority
        enabled = self.inputs.get('enabled')
        if enabled is not None:
            values['enabled'] = bool(enabled)

        db_api.cluster_policy_update(self.context, cluster.id, policy_id,
                                     values)

        return self.RES_OK, _('Policy updated.')

    def _execute(self, cluster):
        # do pre-action policy checking
        self.policy_check(cluster.id, 'BEFORE')
        if self.data['status'] != policy_mod.CHECK_OK:
            return self.RES_ERROR, _('Policy failure: %s') %\
                self.data['reason']

        result = self.RES_OK
        action_name = self.action.lower()
        method_name = action_name.replace('cluster', 'do')
        method = getattr(self, method_name)
        if method is None:
            raise exception.ActionNotSupported(action=self.action)

        result, reason = method(cluster)

        # do post-action policy checking
        if result == self.RES_OK:
            self.policy_check(cluster.id, 'AFTER')
            if self.data['status'] != policy_mod.CHECK_OK:
                return self.RES_ERROR, self.data['reason']

        return result, reason

    def execute(self, **kwargs):
        '''Wrapper of action execution.

        This is mainly a wrapper that executes an action with cluster lock
        acquired.
        :return: A tuple (res, reason) that indicates whether the execution
                 was a success and why if it wasn't a success.
        '''

        try:
            cluster = cluster_mod.Cluster.load(self.context, self.target)
        except exception.ClusterNotFound:
            reason = _LE('Cluster %(id)s not found') % {'id': self.target}
            LOG.error(reason)
            return self.RES_ERROR, reason

        # Try to lock cluster before do real operation
        forced = True if self.action == self.CLUSTER_DELETE else False
        res = senlin_lock.cluster_lock_acquire(cluster.id, self.id,
                                               senlin_lock.CLUSTER_SCOPE,
                                               forced)
        if not res:
            return self.RES_ERROR, _('Failed in locking cluster.')

        try:
            res, reason = self._execute(cluster)
        finally:
            senlin_lock.cluster_lock_release(cluster.id, self.id,
                                             senlin_lock.CLUSTER_SCOPE)

        return res, reason

    def cancel(self):
        return self.RES_OK
