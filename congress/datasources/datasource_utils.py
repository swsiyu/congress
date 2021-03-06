# Copyright (c) 2013,2014 VMware, Inc. All rights reserved.
#
#    Licensed under the Apache License, Version 2.0 (the "License"); you may
#    not use this file except in compliance with the License. You may obtain
#    a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
#    WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
#    License for the specific language governing permissions and limitations
#    under the License.

from congress.datasources import constants


def get_openstack_required_config():
    return {'auth_url': constants.REQUIRED,
            'endpoint': constants.OPTIONAL,
            'region': constants.OPTIONAL,
            'username': constants.REQUIRED,
            'password': constants.REQUIRED,
            'tenant_name': constants.REQUIRED,
            'poll_time': constants.OPTIONAL}
