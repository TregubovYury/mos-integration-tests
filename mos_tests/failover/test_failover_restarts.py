#    Copyright 2016 Mirantis, Inc.
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

import json
import logging
import time

import pytest

from mos_tests.functions.common import wait
from mos_tests.neutron.python_tests.base import TestBase


logger = logging.getLogger(__name__)


@pytest.mark.check_env_('is_ha', 'has_1_or_more_computes')
class TestFailoverRestarts(TestBase):

    def check_common_services(self, openstack_client):
        # check several times that all needed services are up
        for x in range(5):
            for server in self.os_conn.nova.servers.list():
                assert server.status == 'ACTIVE'

            # Get user list and check that all users are enabled
            result = openstack_client('user list --long -f json')
            for user in json.loads(result):
                assert user['Enabled'] is True

            # Get list of services IDs and than description for each service
            result = openstack_client('service list -f json')
            id_list = [service['ID'] for service in json.loads(result)]
            for service_id in id_list:
                cmd = 'service show {} -f json'.format(service_id)
                result = openstack_client(cmd)
                service = json.loads(result)
                assert service['enabled'] is True

            for service in self.os_conn.nova.services.list():
                assert service.status == 'enabled'

            for image in self.os_conn.glance.images.list():
                assert image.status == 'active'

    @pytest.mark.testrail_id('542818')
    def test_restart_galera_services_with_replicaton(self):
        """Restart all Galera services with data replication

        Scenario:
            1. 'pcs resource disable clone_p_mysqld' for stop Galera service.
            2. 'ps -ef | grep mysql' and check that
                all mysql processes was killed.
            3. 'pcs resource enable clone_p_mysqld' for start Galera service.
            4. 'ps -ef | grep mysql' and check that mysql processes worked.
            5.  Execute 'nova list', 'keystone user-list', 'glance image-list',
                'nova-manage service list', 'keystone service-list'
                several times and verify that all works fine.
        """

        def is_mysql_stopped():
            cmd = "ps -ef | grep [m]ysql"
            result = remote.execute(cmd)
            # Mysql stops only after some time
            # Need to wait until all processes go down
            # If the stdout is empty then no mysql processes are run
            if not result['stdout']:
                return True

        def is_mysql_started():
            cmd = "ps -ef | grep \"[/]usr/sbin/mysqld\""
            result = remote.execute(cmd)
            # If /usr/sbin/mysqld is started then mysql available
            if result['stdout']:
                return True

        # Find any controller in cluster
        controller = self.env.get_nodes_by_role('controller')[0]

        with controller.ssh() as remote:

            cmd = 'pcs resource disable clone_p_mysqld'
            logger.info('disable all galera services with cmd {}'.format(cmd))
            remote.check_call(cmd)

            logger.info('wait until all mysql processes are stopped')
            wait(is_mysql_stopped, timeout_seconds=3 * 60, sleep_seconds=5)

            cmd = 'pcs resource enable clone_p_mysqld'
            logger.info('enable all galera services with cmd {}'.format(cmd))
            remote.check_call(cmd)

            logger.info('wait until all mysql processes started')
            wait(is_mysql_started, timeout_seconds=3 * 60, sleep_seconds=5)

    @pytest.mark.testrail_id('542817')
    def test_restart_rabbitmq_services_with_replicaton(self, openstack_client):
        """Restart all RabbitMQ services with data replication
        Scenario
            1. Login to the first Openstack controller node
               and disable RabbitMQ service:
                   pcs resource disable master_p_rabbitmq-server
                   pcs resource disable p_rabbitmq-server
            2. Wait for several seconds and enable the RabbitMQ service back:
                   pcs resource enable master_p_rabbitmq-server
                   pcs resource enable p_rabbitmq-server
            3. Repeat steps 1-2 for all controller nodes in your cluster
            4. Execute 'rabbitmqctl cluster_status' on each contrloller
            5. Verify that all RabbitMQ nodes are in the same cluster.

            The test might fail due to the bug:
                https://bugs.launchpad.net/fuel/+bug/1524024
            The reproduction frequency is about 1/10
        """
        def is_rabbit_alive():
            # Need to check the rabbit service on each controller
            # because on some controller it might take more time to start
            for controller in controllers:
                with controller.ssh() as remote:
                    # The following cmd will return non zero exit code
                    # if rabbit is dead
                    cmd = 'rabbitmqctl cluster_status'
                    result = remote.execute(cmd)
                    if result['exit_code'] != 0:
                        return False
            # Will return True only if the cmd above
            # returned zero exit code for each controller
            return True

        controllers = self.env.get_nodes_by_role('controller')

        logger.info('kill all rabbitmq servers')
        for controller in controllers:
            with controller.ssh() as remote:
                logger.info('disable rabbitmq services on node {}'.format(
                            controller.data['ip']))
                remote.check_call(
                    'pcs resource disable master_p_rabbitmq-server')
                remote.check_call('pcs resource disable p_rabbitmq-server')
                # just a little sleep before enabling the services
                time.sleep(5)
                logger.info('enable rabbitmq services on node {}'.format(
                            controller.data['ip']))
                remote.check_call(
                    'pcs resource enable master_p_rabbitmq-server')
                remote.check_call('pcs resource enable p_rabbitmq-server')

            logger.info('wait until all rabbits come alive')
            wait(is_rabbit_alive, timeout_seconds=10 * 60, sleep_seconds=5)

        logger.info('check that all rabbitmq nodes are in the same cluster')
        # OSTF contain test "RabbitMQ availability"
        # which will check if all rabbit nodes are up or not.
        # Also it was found that restart of rabbit service
        # affects other resources like p_nova_compute_ironic.
        # And full recovery time might take from several up to 15 minutes.
        # So the below call seems to be the best check in this case.
        # It will guaranty that all services are recovered.
        self.env.wait_for_ostf_pass()

        self.check_common_services(openstack_client)

    @pytest.mark.testrail_id('542815')
    def test_instance_folder_after_hard_reboot(self, clean_os):
        """Re-creating instance folder after hard reboot

        Scenario:
            1. Deploy cloud with at least 1 compute node and 3 controllers
            2. Create VM on each compute
            3. Login to each compute node and shutdown it:
                "shutdown now -r"
            4. Perform "hard reboot" for all VMs
            5. Hard reboot should help to recover all VMs
               and they should be in ACTIVE state after that
        """

        # init variables
        zone = self.os_conn.nova.availability_zones.find(zoneName="nova")
        hosts = zone.hosts.keys()
        security_group = self.os_conn.create_sec_group_for_ssh()
        self.instance_keypair = self.os_conn.create_key(key_name='instancekey')

        # create router
        router = self.os_conn.create_router(name="router01")['router']
        logger.info('router {} was created'.format(router['id']))

        # create networks by amount of the compute hosts
        for hostname in hosts:
            net_id = self.os_conn.add_net(router['id'])
            self.os_conn.add_server(net_id,
                                    self.instance_keypair.name,
                                    hostname,
                                    security_group.id)

        logger.info('shutdown all computes in the cluster')
        computes = self.env.get_nodes_by_role('compute')
        for compute in computes:
            with compute.ssh() as remote:
                cmd = 'shutdown now -r'
                remote.check_call(cmd)

        logger.info('Execute hard reboot for the affected servers')
        # That should start back the compute nodes and enable the servers
        for server in self.os_conn.nova.servers.list():
            server.reboot(reboot_type='HARD')

        logger.info('wait until all servers come in ACTIVE state')
        for server in self.os_conn.nova.servers.list():
            wait(lambda: self.os_conn.is_server_active(server),
                 timeout_seconds=10 * 60, sleep_seconds=5)
