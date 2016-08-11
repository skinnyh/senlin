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

from oslo_log import log as logging
import six

from senlin.common import exception
from senlin.common.i18n import _
from senlin.common import schema
from senlin.common import utils
from senlin.drivers import base as driver_base
from senlin.profiles import base

LOG = logging.getLogger(__name__)


class StackProfile(base.Profile):
    '''Profile for an OpenStack Heat stack.

    When this profile is used, the whole cluster is a collection of Heat
    stacks.
    '''

    KEYS = (
        TEMPLATE, CONTEXT, PARAMETERS, FILES,
        TIMEOUT, DISABLE_ROLLBACK, ENVIRONMENT,
    ) = (
        'template', 'context', 'parameters', 'files',
        'timeout', 'disable_rollback', 'environment',
    )

    properties_schema = {
        CONTEXT: schema.Map(
            _('A dictionary for specifying the customized context for '
              'stack operations'),
            default={},
        ),
        TEMPLATE: schema.Map(
            _('Heat stack template.'),
            required=True,
            updatable=True,
        ),
        PARAMETERS: schema.Map(
            _('Parameters to be passed to Heat for stack operations.'),
            default={},
            updatable=True,
        ),
        FILES: schema.Map(
            _('Contents of files referenced by the template, if any.'),
            default={},
            updatable=True,
        ),
        TIMEOUT: schema.Integer(
            _('A integer that specifies the number of minutes that a '
              'stack operation times out.'),
            updatable=True,
        ),
        DISABLE_ROLLBACK: schema.Boolean(
            _('A boolean specifying whether a stack operation can be '
              'rolled back.'),
            default=True,
            updatable=True,
        ),
        ENVIRONMENT: schema.Map(
            _('A map that specifies the environment used for stack '
              'operations.'),
            default={},
            updatable=True,
        )
    }

    OP_NAMES = (
        OP_ABANDON,
    ) = (
        'abandon',
    )

    OPERATIONS = {
        OP_ABANDON: schema.Map(
            _('Abandon a heat stack node.'),
        )
    }

    def __init__(self, type_name, name, **kwargs):
        super(StackProfile, self).__init__(type_name, name, **kwargs)

        self.hc = None
        self.stack_id = None

    def heat(self, obj):
        """Construct heat client using the combined context.

        :param obj: Node object to operate on.
        :returns: A reference to the orchestration driver.
        """
        if self.hc:
            return self.hc
        params = self._build_conn_params(obj.user, obj.project)
        self.hc = driver_base.SenlinDriver().orchestration(params)
        return self.hc

    def do_validate(self, obj):
        """Validate the stack template used by a node.

        :param obj: Node object to operate.
        :returns: True if validation succeeds.
        :raises: `InvalidSpec` exception is raised if template is invalid.
        """
        kwargs = {
            'stack_name': obj.name,
            'template': self.properties[self.TEMPLATE],
            'timeout_mins': self.properties[self.TIMEOUT],
            'disable_rollback': self.properties[self.DISABLE_ROLLBACK],
            'parameters': self.properties[self.PARAMETERS],
            'files': self.properties[self.FILES],
            'environment': self.properties[self.ENVIRONMENT],
        }
        try:
            self.heat(obj).validate_template(**kwargs)
        except exception.InternalError as ex:
            msg = _('Failed in validating template: %s') % six.text_type(ex)
            raise exception.InvalidSpec(message=msg)

        return True

    def do_create(self, obj):
        """Create a heat stack using the given node object.

        :param obj: The node object to operate on.
        :returns: The UUID of the heat stack created.
        """
        kwargs = {
            'stack_name': obj.name + '-' + utils.random_name(8),
            'template': self.properties[self.TEMPLATE],
            'timeout_mins': self.properties[self.TIMEOUT],
            'disable_rollback': self.properties[self.DISABLE_ROLLBACK],
            'parameters': self.properties[self.PARAMETERS],
            'files': self.properties[self.FILES],
            'environment': self.properties[self.ENVIRONMENT],
        }

        try:
            stack = self.heat(obj).stack_create(**kwargs)

            # Timeout = None means we will use the 'default_action_timeout'
            # It can be overridden by the TIMEOUT profile propertie
            timeout = None
            if self.properties[self.TIMEOUT]:
                timeout = self.properties[self.TIMEOUT] * 60

            self.heat(obj).wait_for_stack(stack.id, 'CREATE_COMPLETE',
                                          timeout=timeout)
            return stack.id
        except exception.InternalError as ex:
            LOG.error(_('Failed in creating stack: %s'), six.text_type(ex))
            return None

    def do_delete(self, obj):
        """Delete the physical stack behind the node object.

        :param obj: The node object to operate on.
        :returns: A boolean indicating whether the operation was successful.
        """
        self.stack_id = obj.physical_id

        try:
            self.heat(obj).stack_delete(self.stack_id, True)
            self.heat(obj).wait_for_stack_delete(self.stack_id)
        except exception.InternalError as ex:
            LOG.error('Faild in deleting stack: %s' % six.text_type(ex))
            return False

        return True

    def do_update(self, obj, new_profile, **params):
        """Perform update on object.

        :param obj: the node object to operate on
        :param new_profile: the new profile used for updating
        :param params: other parameters for the update request.
        :returns: A boolean indicating whether the operation is successful.
        """
        self.stack_id = obj.physical_id
        if not self.stack_id:
            return False

        if not self.validate_for_update(new_profile):
            return False

        fields = {}
        new_template = new_profile.properties[new_profile.TEMPLATE]
        if new_template != self.properties[self.TEMPLATE]:
            fields['template'] = new_template

        new_params = new_profile.properties[new_profile.PARAMETERS]
        if new_params != self.properties[self.PARAMETERS]:
            fields['parameters'] = new_params

        new_timeout = new_profile.properties[new_profile.TIMEOUT]
        if new_timeout != self.properties[self.TIMEOUT]:
            fields['timeout_mins'] = new_timeout

        new_dr = new_profile.properties[new_profile.DISABLE_ROLLBACK]
        if new_dr != self.properties[self.DISABLE_ROLLBACK]:
            fields['disable_rollback'] = new_dr

        new_files = new_profile.properties[new_profile.FILES]
        if new_files != self.properties[self.FILES]:
            fields['files'] = new_files

        new_environment = new_profile.properties[new_profile.ENVIRONMENT]
        if new_environment != self.properties[self.ENVIRONMENT]:
            fields['environment'] = new_environment

        if not fields:
            return True

        try:
            # Timeout = None means we will use the 'default_action_timeout'
            # It can be overridden by the TIMEOUT profile propertie
            timeout = None
            if self.properties[self.TIMEOUT]:
                timeout = self.properties[self.TIMEOUT] * 60
            self.heat(obj).stack_update(self.stack_id, **fields)
            self.heat(obj).wait_for_stack(self.stack_id, 'UPDATE_COMPLETE',
                                          timeout=timeout)
        except exception.InternalError as ex:
            LOG.error(_('Failed in updating stack: %s'), six.text_type(ex))
            return False

        return True

    def do_check(self, obj):
        """Check stack status.

        :param obj: Node object to operate.
        :returns: True if check succeedes, or False otherwise.
        """
        stack_id = obj.physical_id
        if stack_id is None:
            return False

        hc = self.heat(obj)
        try:
            # Timeout = None means we will use the 'default_action_timeout'
            # It can be overridden by the TIMEOUT profile propertie
            timeout = None
            if self.properties[self.TIMEOUT]:
                timeout = self.properties[self.TIMEOUT] * 60
            hc.stack_check(stack_id)
            hc.wait_for_stack(stack_id, 'CHECK_COMPLETE', timeout=timeout)
        except exception.InternalError as ex:
            LOG.error(_('Failed in checking stack: %s.'), six.text_type(ex))
            return False

        return True

    def do_get_details(self, obj):
        if not obj.physical_id:
            return {}

        return self.heat(obj).stack_get(obj.physical_id)

    def handle_abandon(self, obj, **options):
        """Handler for abandoning a heat stack node."""
        pass
