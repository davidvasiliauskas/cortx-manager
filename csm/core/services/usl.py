#!/usr/bin/env python3

"""
 ****************************************************************************
 Filename:          usl.py
 Description:       Services for USL calls

 Creation Date:     10/21/2019
 Author:            Alexander Voronov

 Do NOT modify or remove this copyright and confidentiality notice!
 Copyright (c) 2001 - $Date: 2015/01/14 $ Seagate Technology, LLC.
 The code contained herein is CONFIDENTIAL to Seagate Technology, LLC.
 Portions are also trade secret. Any use, duplication, derivation, distribution
 or disclosure of this code, for any reason, not expressly authorized is
 prohibited. All other rights are expressly reserved by Seagate Technology, LLC.
 ****************************************************************************
"""

import asyncio
import time

from aiohttp import web, ClientSession, TCPConnector
from boto.s3.bucket import Bucket
from botocore.exceptions import ClientError
from datetime import date
from random import SystemRandom
from marshmallow import ValidationError
from marshmallow.validate import URL
from typing import cast, Any, Dict, List, Optional, Tuple
from uuid import UUID, uuid5

from csm.common.conf import Conf
from csm.common.errors import (
    CsmGatewayTimeout, CsmInternalError, CsmNotFoundError, CsmServiceConflict
)
from eos.utils.log import Log
from csm.common.services import ApplicationService
from csm.core.blogic import const
from csm.core.services.sessions import S3Credentials
from csm.core.services.s3.accounts import S3AccountService
from csm.core.data.models.s3 import IamUser, IamUserCredentials
from csm.core.data.models.usl import Device, Volume
from csm.core.services.s3.utils import CsmS3ConfigurationFactory, S3ServiceError
from csm.core.services.usl_certificate_manager import (
    USLDomainCertificateManager, USLNativeCertificateManager, CertificateError
)
from eos.utils.security.cipher import Cipher
from eos.utils.security.secure_storage import SecureStorage

DEFAULT_EOS_DEVICE_VENDOR = 'Seagate'


class UslService(ApplicationService):
    """
    Implements USL service operations.
    """
    # FIXME improve token management
    _token: str
    _s3plugin: Any
    _device: Device
    _domain_certificate_manager: USLDomainCertificateManager
    _native_certificate_manager: USLNativeCertificateManager

    def __init__(self, s3_plugin, storage, provisioner) -> None:
        """
        Constructor.
        """
        self._token = ''
        self._s3plugin = s3_plugin
        self._provisioner = provisioner
        dev_uuid = self._get_device_uuid()
        self._device = Device.instantiate(
            self._get_system_friendly_name(),
            '0000',
            str(dev_uuid),
            'S3',
            dev_uuid,
            DEFAULT_EOS_DEVICE_VENDOR,
        )
        # FIXME: the provisioner call fails on the build machine, so can't get the cluster id
        # Use the device UUID as a temporary solution
        # loop = asyncio.get_event_loop()
        # cluster_id = loop.run_until_complete(self._provisioner.get_cluster_id())
        # key = Cipher.generate_key(cluster_id, str(dev_uuid))
        key = Cipher.generate_key(str(dev_uuid), 'USL')
        secure_storage = SecureStorage(storage, key)
        self._domain_certificate_manager = USLDomainCertificateManager(secure_storage)
        self._native_certificate_manager = USLNativeCertificateManager()

    def _get_system_friendly_name(self) -> str:
        return str(Conf.get(const.CSM_GLOBAL_INDEX, 'PRODUCT.friendly_name') or 'local')

    def _get_device_uuid(self) -> UUID:
        """Obtains the EOS device UUID from config."""

        return UUID(Conf.get(const.CSM_GLOBAL_INDEX, "PRODUCT.uuid"))

    def _format_udx_register_device_params(self, url: str, pin: str, token: str) -> Dict[str, str]:
        register_device_params = {
            'url': url,
            'regPin': pin,
            'regToken': token,
        }
        return register_device_params

    async def _format_udx_access_params(
        self, s3_account: Dict, iam_user: IamUser, iam_user_access_key: IamUserCredentials,
        bucket: Bucket
    ) -> Dict[str, Any]:
        network = await self._provisioner.get_network_configuration()
        access_params = {
            'accountName': s3_account.get('account_name'),
            # TODO find a better way to obtain S3 server host and port
            'uri': 's3://{}:{}'.format(
                network.cluster_ip,
                Conf.get(const.CSM_GLOBAL_INDEX, 'S3.s3_port'),
            ),
            'credentials': {
                'accessKey': iam_user_access_key.access_key_id,
                'secretKey': iam_user_access_key.secret_key,
            },
        }
        return access_params

    async def _cleanup_on_udx_registration_error(
        self, s3_account: Dict, iam_user: IamUser, iam_user_access_key: IamUserCredentials,
        bucket: Bucket
    ) -> None:
        Log.debug('Cleaning up UDX resources...')
        access_key = s3_account.get('access_key')
        secret_key = s3_account.get('secret_key')
        Log.debug('Remove bucket')
        s3_client = self._s3plugin.get_s3_client(
            access_key, secret_key, CsmS3ConfigurationFactory.get_s3_connection_config()
        )
        await s3_client.delete_bucket(bucket.name)
        Log.debug('Remove IAM user access key')
        iam_client = self._s3plugin.get_iam_client(
            access_key, secret_key, CsmS3ConfigurationFactory.get_iam_connection_config()
        )
        await iam_client.delete_user_access_key(
            iam_user.user_name, iam_user_access_key.access_key_id)
        Log.debug('Remove IAM user')
        await iam_client.delete_user(iam_user.user_name)
        Log.debug('Remove S3 account')
        s3_account_service = S3AccountService(self._s3plugin)
        s3_credentials = S3Credentials(
            str(s3_account.get('account_name')),
            str(s3_account.get('access_key')),
            str(s3_account.get('secret_key')),
            '',
        )
        try:
            await s3_account_service.delete_account(
                s3_credentials, s3_credentials.user_id
            )
        except (S3ServiceError, Exception) as e:
            reason = f'Failed to remove account {s3_credentials.user_id}'
            Log.error(f'{reason} --- {e}')
        Log.info('UDX resources cleanup complete.')

    async def _initialize_udx_s3_resources(
        self,
        s3_account_name: str,
        s3_account_email: str,
        s3_account_password: str,
        iam_user_name: str,
        iam_user_password: str,
        bucket_name: str,
        *,
        repair_mode: bool = False,
    ) -> Tuple[Dict, IamUser, IamUserCredentials, Bucket]:
        s3_account: Optional[Dict] = None
        iam_user: Optional[IamUser] = None
        existing_iam_user: Optional[IamUser] = None
        iam_user_access_key: Optional[IamUserCredentials] = None
        bucket: Optional[Bucket] = None
        existing_bucket: Optional[Bucket] = None

        async def cleanup(*, s3_account_service=None, iam_client=None, s3_client=None):
            # Remove bucket if it exists
            if s3_client is not None and bucket is not None:
                await s3_client.delete_bucket(bucket.name)
            # Remove IAM user's access key if it exists
            if iam_client is not None and iam_user_access_key is not None:
                await iam_client.delete_user_access_key(
                    iam_user.user_name,
                    iam_user_access_key.access_key_id)
            # Remove IAM user if it exists
            if iam_client is not None and iam_user is not None:
                await iam_client.delete_user(iam_user.user_name)
            # Remove S3 account if it exists
            if s3_account_service is not None and s3_account is not None:
                s3_credentials = S3Credentials(
                    str(s3_account.get('account_name')),
                    str(s3_account.get('access_key')),
                    str(s3_account.get('secret_key')),
                    '',
                )
                try:
                    await s3_account_service.delete_account(
                        s3_credentials, s3_credentials.user_id
                    )
                except (S3ServiceError, Exception) as e:
                    reason = f'Failed to remove account {s3_credentials.user_id}'
                    Log.error(f'{reason} --- {e}')

        Log.debug('Create S3 account')
        try:
            s3_account_service = S3AccountService(self._s3plugin)
            s3_account = await s3_account_service.create_account(
                s3_account_name, s3_account_email, s3_account_password,
            )
            # FIXME `cast()` is unsafe; mypy could not derive type correctly
            s3_account = cast(Dict, s3_account)
            access_key = s3_account.get('access_key')
            secret_key = s3_account.get('secret_key')
            assert s3_account is not None
        # FIXME There is no way to extract meaningful error codes from ``CsmInternalError``.
        #   For that reason, we raise internal server errors in all of those cases.
        except (S3ServiceError, CsmInternalError, Exception) as e:
            await cleanup(s3_account_service=s3_account_service)
            reason = 'S3 account creation failed'
            Log.error(f'{reason}---{str(e)}')
            raise CsmInternalError(desc=reason)
        Log.debug('Create IAM user')
        try:
            iam_client = self._s3plugin.get_iam_client(
                access_key, secret_key, CsmS3ConfigurationFactory.get_iam_connection_config()
            )
            existing_iam_user = await self._get_udx_iam_user(iam_client, iam_user_name)
            if existing_iam_user is None:
                iam_user = await self._create_udx_iam_user(
                    iam_client, iam_user_name, iam_user_password
                )
            elif not repair_mode:
                reason = 'IAM user already exists'
                Log.error(f'{reason}')
                raise CsmServiceConflict(desc=reason)
            assert existing_iam_user is not None or iam_user is not None
        except CsmServiceConflict as e:
            await cleanup(s3_account_service=s3_account_service, iam_client=iam_client)
            raise e
        except Exception as e:
            await cleanup(s3_account_service=s3_account_service, iam_client=iam_client)
            reason = 'IAM user creation failed'
            Log.error(f'{reason}---{str(e)}')
            raise CsmInternalError(desc=reason)
        Log.debug('Create access key for IAM user')
        try:
            # Rationale: keep only one access key for UDX IAM user
            if repair_mode:
                await self._delete_udx_iam_user_credentials(iam_client, iam_user.user_name)
            iam_user_access_key = await self._create_udx_iam_user_credentials(
                iam_client, iam_user_name)
        except CsmInternalError as e:
            await cleanup(s3_account_service=s3_account_service, iam_client=iam_client)
            reason = 'IAM user access key creation failed'
            Log.error(f'{reason}---{str(e)}')
            raise CsmInternalError(desc=reason)
        Log.debug('Create bucket')
        try:
            s3_client = self._s3plugin.get_s3_client(
                access_key, secret_key, CsmS3ConfigurationFactory.get_s3_connection_config()
            )
            existing_bucket = await self._get_udx_bucket(s3_client, bucket_name)
            if existing_bucket is None:
                bucket = await self._create_udx_bucket(s3_client, bucket_name)
                await self._tag_udx_bucket(s3_client, bucket_name)
                await self._set_udx_policy(s3_client, iam_user, bucket_name)
            elif not repair_mode:
                reason = 'Bucket already exists'
                Log.error(f'{reason}')
                raise CsmServiceConflict(desc=reason)
        except CsmServiceConflict as e:
            await cleanup(
                s3_account_service=s3_account_service, iam_client=iam_client, s3_client=s3_client
            )
            raise e
        except Exception as e:
            await cleanup(
                s3_account_service=s3_account_service, iam_client=iam_client, s3_client=s3_client
            )
            reason = 'Bucket creation failed'
            Log.error(f'{reason}---{str(e)}')
            raise CsmInternalError(desc=reason)
        # In case we are running on repair mode, suffice to test if the resources already exist.
        # In case we are not, exceptions should have been raised previously.
        # FIXME `cast()` is unsafe; mypy could not derive types correctly
        return (
            s3_account,
            cast(IamUser, existing_iam_user or iam_user),
            iam_user_access_key,
            cast(Bucket, existing_bucket or bucket),
        )

    async def _get_udx_iam_user(self, iam_cli, user_name: str) -> Optional[IamUser]:
        """
        Checks UDX IAM user exists and returns it
        """

        # TODO: Currently the IAM server does not support 'get-user' operation.
        # Thus there is no way to obtain details about existing IAM user: ID and ARN.
        # Workaround: delete IAM user even if it exists (and recreate then).
        # When get-user is implemented on the IAM server side workaround could be removed.
        try:
            await iam_cli.delete_user(user_name)
        except ClientError:
            # Ignore errors in deletion, user might not exist
            pass

        return None

    async def _create_udx_iam_user(self, iam_cli, user_name: str, user_passwd: str) -> IamUser:
        """
        Creates UDX IAM user inside the currently logged in S3 account
        """

        Log.debug(f'Creating UDX IAM user {user_name}')
        iam_user_resp = await iam_cli.create_user(user_name)
        if hasattr(iam_user_resp, "error_code"):
            erorr_msg = iam_user_resp.error_message
            raise CsmInternalError(f'Failed to create UDX IAM user: {erorr_msg}')
        Log.info(f'UDX IAM user {user_name} is created')

        iam_login_resp = await iam_cli.create_user_login_profile(user_name, user_passwd, False)
        if hasattr(iam_login_resp, "error_code"):
            # Remove the user if the login profile creation failed
            await iam_cli.delete_user(user_name)
            error_msg = iam_login_resp.error_message
            raise CsmInternalError(f'Failed to create login profile for UDX IAM user {error_msg}')
        Log.info(f'Login profile for UDX IAM user {user_name} is created')

        return iam_user_resp

    async def _create_udx_iam_user_credentials(self, iam_cli, user_name: str) -> IamUserCredentials:
        """
        Gets the access key id and secret key for UDX IAM user
        """

        creds = await iam_cli.create_user_access_key(user_name)
        if hasattr(creds, 'error_code'):
            error_msg = creds.error_message
            raise CsmInternalError(f'Failed to create access key for UDX IAM user: {error_msg}')
        return creds

    async def _delete_udx_iam_user_credentials(self, iam_cli, user_name: str):
        Log.debug(f'Deleting UDX IAM user {user_name} credentials')
        access_keys_resp = await iam_cli.list_user_access_keys(user_name)
        for access_key in access_keys_resp.access_keys:
            res = await iam_cli.delete_user_access_key(user_name, access_key.access_key_id)
            if hasattr(res, 'error_code'):
                error_msg = res.error_message
                raise CsmInternalError(f'Failed to delete access key {access_key.access_key_id}: '
                                       f'{error_msg}')
        Log.info(f'IAM user {user_name} credentials are deleted')

    async def _get_udx_bucket(self, s3_cli, bucket_name: str):
        """
        Checks if UDX bucket already exists and returns it
        """

        Log.debug(f'Getting UDX bucket')
        bucket = await s3_cli.get_bucket(bucket_name)

        return bucket

    async def _create_udx_bucket(self, s3_cli, bucket_name: str) -> Bucket:
        """
        Creates UDX bucket inside the curretnly logged in S3 account
        """

        Log.debug(f'Creating UDX bucket {bucket_name}')
        bucket = await s3_cli.create_bucket(bucket_name)
        Log.info(f'UDX bucket {bucket_name} is created')
        return bucket

    async def _is_bucket_udx_enabled(self, s3_cli, bucket):
        """
        Checks if bucket is UDX enabled

        The UDX enabled bucket contains tag {Key=udx,Value=enabled}
        """

        tags = await s3_cli.get_bucket_tagging(bucket)
        return tags.get('udx', 'disabled') == 'enabled'

    async def _tag_udx_bucket(self, s3_cli, bucket_name: str):
        """
        Puts the UDX tag on a specified bucket
        """

        Log.debug(f'Tagging bucket {bucket_name} with UDX tag')
        bucket_tags = {"udx": "enabled"}
        await s3_cli.put_bucket_tagging(bucket_name, bucket_tags)
        Log.info(f'UDX bucket {bucket_name} is taggged with {bucket_tags}')

    async def _set_udx_policy(self, s3_cli, iam_user, bucket_name: str):
        """
        Grants the specified IAM user full access to the specified bucket
        """

        Log.debug(f'Setting UDX policy for bucket {bucket_name} and IAM user {iam_user.user_name}')
        policy = {
            'Version': str(date.today()),
            'Statement': [{
                'Sid': 'UdxIamAccountPerm',
                'Effect': 'Allow',
                'Principal': {"AWS": iam_user.arn},
                'Action': ['s3:GetObject', 's3:PutObject',
                           's3:ListMultipartUploadParts', 's3:AbortMultipartUpload',
                           's3:GetObjectAcl', 's3:PutObjectAcl',
                           's3:PutObjectTagging',
                           # TODO: now S3 server rejects the following policies
                           # 's3:DeleteObject', 's3:RestoreObject', 's3:DeleteObjectTagging',
                           ],
                'Resource': f'arn:aws:s3:::{bucket_name}/*',
            }]
        }
        await s3_cli.put_bucket_policy(bucket_name, policy)
        Log.info(f'UDX policy is set for bucket {bucket_name} and IAM user {iam_user.user_name}')

    def _get_volume_name(self, bucket_name: str) -> UUID:
        return self._get_system_friendly_name() + ": " + bucket_name

    def _get_volume_uuid(self, bucket_name: str) -> UUID:
        """Generates the EOS volume (bucket) UUID from EOS device UUID and bucket name."""
        return uuid5(self._device.uuid, bucket_name)

    async def _get_udx_volume_list(
        self, access_key_id: str, secret_access_key: str
    ) -> Dict[UUID, Volume]:
        s3_client = self._s3plugin.get_s3_client(
            access_key_id, secret_access_key, CsmS3ConfigurationFactory.get_s3_connection_config()
        )
        device_uuid = self._device.uuid
        volumes = {}
        for bucket in await s3_client.get_all_buckets():
            bucket_name = bucket.name
            if await self._is_bucket_udx_enabled(s3_client, bucket):
                volume_uuid = self._get_volume_uuid(bucket_name)
                volume_name = self._get_volume_name(bucket_name)
                volume = Volume.instantiate(volume_name, bucket_name, device_uuid, volume_uuid)
                volumes[volume_uuid] = volume
        return volumes

    async def get_device_list(self) -> List[Dict[str, str]]:
        """
        Provides a list with all available devices.

        :return: A list with dictionaries, each containing information about a specific device.
        """

        return [self._device.to_primitive()]

    async def get_device_volumes_list(
        self, device_id: UUID, uri: str, access_key_id: str, secret_access_key: str
    ) -> List[Dict[str, Any]]:
        """
        Provides a list of all volumes associated to a specific device.

        :param device_id: Device UUID
        :param uri: URI to storage service
        :param access_key_id: Access key ID to storage service
        :param secret_access_key: Secret access key to storage service
        :return: A list with dictionaries, each containing information about a specific volume.
        """

        if device_id != self._device.uuid:
            raise CsmNotFoundError(desc=f'Device with ID {device_id} is not found')
        volumes = await self._get_udx_volume_list(access_key_id, secret_access_key)
        return [v.to_primitive(role='public') for _,v in volumes.items()]

    async def _handle_udx_volume_mount_umount(
        self, device_id: UUID, volume_id: UUID, uri: str,
        access_key_id: str, secret_access_key: str, mount=True,
    ) -> Dict[str, str]:
        """
        Handles UDX volume mount/umount

        Checks the device and the volume with the specified IDs exist and return
        the required mount/umount information
        """
        if device_id != self._device.uuid:
            raise CsmNotFoundError(desc=f'Device with ID {device_id} is not found')
        volumes = await self._get_udx_volume_list(access_key_id, secret_access_key)
        if volume_id not in volumes:
            raise CsmNotFoundError(desc=f'Volume with ID {volume_id} is not found '
                                        f'on the device with ID {device_id}')
        if mount:
            return {
                'mountPath': volumes[volume_id].bucketName,
                'handle': volumes[volume_id].bucketName
            }
        else:
            return volumes[volume_id].bucketName

    async def post_device_volume_mount(
        self, device_id: UUID, volume_id: UUID, uri: str, access_key_id: str, secret_access_key: str
    ) -> Dict[str, str]:
        """
        Attaches a volume associated to a specific device to a mount point.

        :param device_id: Device UUID
        :param volume_id: Volume UUID
        :param uri: URI to storage service
        :param access_key_id: Access key ID to storage service
        :param secret_access_key: Secret access key to storage service
        :return: A dictionary containing the mount handle and the mount path.
        """
        return await self._handle_udx_volume_mount_umount(
            device_id, volume_id, uri, access_key_id, secret_access_key, mount=True)

    async def post_device_volume_unmount(
        self, device_id: UUID, volume_id: UUID, uri: str, access_key_id: str, secret_access_key: str
    ) -> str:
        """
        Detaches a volume associated to a specific device from its current mount point.

        The current implementation reflects the API specification but does nothing.

        :param device_id: Device UUID
        :param volume_id: Volume UUID
        :param uri: URI to storage service
        :param access_key_id: Access key ID to storage service
        :param secret_access_key: Secret access key to storage service
        :return: The volume's mount handle
        """
        return await self._handle_udx_volume_mount_umount(
            device_id, volume_id, uri, access_key_id, secret_access_key, mount=False)

    async def get_events(self) -> None:
        """
        Returns USL events one-by-one
        """
        # TODO implement
        pass

    async def post_register_device(
        self,
        url: str,
        pin: str,
        s3_account_name: str,
        s3_account_email: str,
        s3_account_password: str,
        iam_user_name: str,
        iam_user_password: str,
        bucket_name: str,
    ) -> Dict:
        """
        Executes device registration sequence. Communicates with the UDS server in order to start
        registration and verify its status.

        :param url: Registration URL as provided by the UDX portal
        :param pin: Registration PIN as provided by the UDX portal
        """
        uds_url = Conf.get(const.CSM_GLOBAL_INDEX, 'UDS.url') or const.UDS_SERVER_DEFAULT_BASE_URL
        try:
            validate_url = URL(schemes=('http', 'https'))
            validate_url(uds_url)
        except ValidationError:
            reason = 'UDS base URL is not valid'
            Log.error(reason)
            raise CsmInternalError(desc=reason)
        # Let ``_initialize_udx_s3_resources()`` propagate its exceptions
        (udx_s3_account, udx_iam_user, udx_iam_user_access_key,
         udx_bucket) = await self._initialize_udx_s3_resources(
            s3_account_name,
            s3_account_email,
            s3_account_password,
            iam_user_name,
            iam_user_password,
            bucket_name,
        )
        try:
            register_device_params = self._format_udx_register_device_params(url, pin, self._token)
            access_params = await self._format_udx_access_params(
                udx_s3_account, udx_iam_user, udx_iam_user_access_key, udx_bucket
            )
            registration_body = {
                'registerDeviceParams': register_device_params,
                'accessParams': access_params,
            }
        except Exception as e:
            await self._cleanup_on_udx_registration_error(
                udx_s3_account, udx_iam_user, udx_iam_user_access_key, udx_bucket)
            reason = 'Error on registration body assembly'
            Log.error(f'{reason}---{str(e)}')
            raise CsmInternalError(desc=reason)
        try:
            endpoint_url = str(uds_url) + '/uds/v1/registration/RegisterDevice'
            # FIXME add relevant certificates to SSL context instead of disabling validation
            async with ClientSession(connector=TCPConnector(verify_ssl=False)) as session:
                Log.info(f'Start device registration at {uds_url}')
                async with session.put(endpoint_url, json=registration_body) as response:
                    if response.status != 201:
                        reason = 'Could not start device registration'
                        Log.error(f'{reason}---unexpected status code {response.status}')
                        raise CsmInternalError(desc=reason)
                Log.info('Device registration in process---waiting for confirmation')
                timeout_limit = time.time() + 60
                while time.time() < timeout_limit:
                    async with session.get(endpoint_url) as response:
                        if response.status == 200:
                            Log.info('Device registration successful')
                            break
                        elif response.status != 201:
                            reason = 'Device registration failed'
                            Log.error(f'{reason}---unexpected status code {response.status}')
                            raise CsmInternalError(desc=reason)
                    await asyncio.sleep(1)
                else:
                    reason = 'Could not confirm device registration status'
                    Log.error(reason)
                    raise CsmGatewayTimeout(desc=reason)
                return {
                    's3_account': {
                        'access_key': udx_s3_account['access_key'],
                        'secret_key': udx_s3_account['secret_key'],
                    },
                    'iam_user': {
                        'access_key': access_params['credentials']['accessKey'],
                        'secret_key': access_params['credentials']['secretKey'],
                    },
                }
        except Exception as e:
            await self._cleanup_on_udx_registration_error(
                udx_s3_account, udx_iam_user, udx_iam_user_access_key, udx_bucket)
            raise e

    async def get_register_device(self) -> None:
        uds_url = Conf.get(const.CSM_GLOBAL_INDEX, 'UDS.url') or const.UDS_SERVER_DEFAULT_BASE_URL
        endpoint_url = str(uds_url) + '/uds/v1/registration/RegisterDevice'
        # FIXME add relevant certificates to SSL context instead of disabling validation
        async with ClientSession(connector=TCPConnector(verify_ssl=False)) as session:
            async with session.get(endpoint_url) as response:
                if response.status != 200:
                    raise CsmNotFoundError()

    # TODO replace stub
    async def get_registration_token(self) -> Dict[str, str]:
        """
        Generates a random registration token.

        :return: A 12-digit token.
        """
        self._token = ''.join(SystemRandom().sample('0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ', 12))
        return {'registrationToken': self._token}

    async def _get_mgmt_url(self) -> Dict[str, str]:
        """
        Returns a management link to be provided to the UDS.

        :return: a dictionary with a management link's name and value.
        """
        network = await self._provisioner.get_network_configuration()
        mgmt_url = {
            'name': 'mgmtUrl',
            'url': f'https://{network.mgmt_vip}',
        }
        return mgmt_url

    async def _get_service_urls(self) -> List[Dict[str, str]]:
        """
        Gathers all service URLs to be provided to the UDS.

        :return: a list of service URLs.
        """

        service_urls = []
        mgmt_url = await self._get_mgmt_url()
        service_urls.append(mgmt_url)
        return service_urls

    # TODO replace stub
    async def get_system(self) -> Dict[str, str]:
        """
        Provides information about the system.

        :return: A dictionary containing system information.
        """
        friendly_name = self._get_system_friendly_name()
        service_urls = await self._get_service_urls()
        return {
            'model': 'EES',
            'type': 'ees',
            'serialNumber': self._device.uuid,
            'friendlyName': friendly_name,
            'firmwareVersion': '0.00',
            'serviceUrls': service_urls,
        }

    async def post_system_certificates(self) -> web.Response:
        """
        Create USL domain key pair in case it does not exist.

        :returns: USL public key as an ``application/octet-stream`` HTTP response
        """
        if await self._domain_certificate_manager.get_private_key_bytes() is not None:
            raise web.HTTPForbidden()
        await self._domain_certificate_manager.create_private_key_file(overwrite=False)
        private_key_bytes = await self._domain_certificate_manager.get_private_key_bytes()
        if private_key_bytes is None:
            reason = 'Could not read USL private key'
            Log.error(reason)
            raise web.HTTPInternalServerError(reason=reason)
        body = await self._domain_certificate_manager.get_public_key_bytes()
        if body is None:
            reason = 'Could not read USL public key'
            Log.error(f'{reason}')
            raise web.HTTPInternalServerError(reason=reason)
        return web.Response(body=body)

    async def put_system_certificates(self, certificate: bytes) -> None:
        if await self._domain_certificate_manager.get_certificate_bytes() is not None:
            raise web.HTTPForbidden()
        try:
            await self._domain_certificate_manager.create_certificate_file(certificate)
        except CertificateError as e:
            reason = 'Could not update USL certificate'
            Log.error(f'{reason}: {e}')
            raise web.HTTPInternalServerError(reason=reason)
        raise web.HTTPNoContent()

    async def delete_system_certificates(self) -> None:
        """
        Delete all key material related with the USL domain certificate.
        """
        deleted = await self._domain_certificate_manager.delete_key_material()
        if not deleted:
            raise web.HTTPForbidden() from None
        # Don't return 200 on success, but 204 as USL API specification requires
        raise web.HTTPNoContent()

    async def get_system_certificates_by_type(self, material_type: str) -> web.Response:
        """
        Provides key material according to the specified type.

        :param material_type: Key material type
        :return: The corresponding key material as an ``application/octet-stream`` HTTP response
        """
        get_material_bytes = {
            'domainCertificate': self._domain_certificate_manager.get_certificate_bytes,
            'domainPrivateKey': self._domain_certificate_manager.get_private_key_bytes,
            'nativeCertificate': self._native_certificate_manager.get_certificate_bytes,
            'nativePrivateKey': self._native_certificate_manager.get_private_key_bytes,
        }.get(material_type)
        if get_material_bytes is None:
            reason = f'Unexpected key material type "{material_type}"'
            Log.error(reason)
            raise web.HTTPInternalServerError(reason=reason)
        body = await get_material_bytes()
        if body is None:
            raise web.HTTPNotFound()
        return web.Response(body=body)

    # TODO replace stub
    async def get_network_interfaces(self) -> List[Dict[str, Any]]:
        """
        Provides a list of all network interfaces in a system.

        :return: A list containing dictionaries, each containing information about a specific
            network interface.
        """
        return [
            {
                'name': 'tbd',
                'type': 'tbd',
                'macAddress': 'AA:BB:CC:DD:EE:FF',
                'isActive': True,
                'isLoopback': False,
                'ipv4': '127.0.0.1',
                'netmask': '255.0.0.0',
                'broadcast': '127.255.255.255',
                'gateway': '127.255.255.254',
                'ipv6': '::1',
                'link': 'tbd',
                'duplex': 'tbd',
                'speed': 0,
            }
        ]
