import getpass
import sys
import json
import argparse
import base64
import datetime
import uuid
import asyncio
import binascii
import time
import httpx
import codecs
from urllib.parse import urlparse, parse_qs, quote_plus, urlencode
#from urllib3.util import SKIP_HEADER
from xml.sax.saxutils import escape as xml_escape
import xml.etree.ElementTree as ET
from xml.dom.minidom import parseString
import os
from cryptography.hazmat.primitives import serialization, padding, hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.kbkdf import CounterLocation, KBKDFHMAC, Mode
from cryptography.hazmat.backends import default_backend
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from aroadtools.roadlib.constants import WELLKNOWN_RESOURCES, WELLKNOWN_CLIENTS, WELLKNOWN_USER_AGENTS, \
    DSSO_BODY_KERBEROS, DSSO_BODY_USERPASS
#import adal
import jwt
from aroadtools.roadlib.utils import printhook

def get_data(data):
    return base64.urlsafe_b64decode(data+('='*(len(data)%4)))

class AuthenticationException(Exception):
    """
    Generic exception we can throw when auth fails so that the error
    goes back to the user.
    """

class Authentication():
    """
    Authentication class for ROADtools
    """
    def __init__(self, username=None, password=None, tenant=None, client_id='1b730954-1685-4b74-9bfd-dac224a7b894', httptransport=None, httpauth = None, printhook=printhook):
        self.username = username
        self.password = password
        self.tenant = tenant
        self.client_id = None
        self.set_client_id(client_id)
        self.resource_uri = 'https://graph.windows.net/'
        self.tokendata = {}
        self.refresh_token = None
        self.saml_token = None
        self.access_token = None
        self.proxies = None
        self.verify = True
        self.outfile = None
        self.debug = False
        self.scope = None
        self.user_agent = None
        self.httptransport = httptransport
        self.httpauth = httpauth
        self.print=printhook

    def get_authority_url(self, default_tenant='common'):
        """
        Returns the authority URL for the tenant specified, or the
        common one if no tenant was specified
        """
        if self.tenant is not None:
            return f'https://login.microsoftonline.com/{self.tenant}'
        return f'https://login.microsoftonline.com/{default_tenant}'

    def set_client_id(self, clid):
        """
        Sets client ID to use (accepts aliases)
        """
        self.client_id = self.lookup_client_id(clid)

    def set_resource_uri(self, uri):
        """
        Sets resource URI to use (accepts aliases)
        """
        self.resource_uri = self.lookup_resource_uri(uri)

    def set_user_agent(self, useragent):
        """
        Overrides user agent (accepts aliases)
        """
        self.user_agent = self.lookup_user_agent(useragent)
        # Patch it in adal too in case we fall back to that
        #pylint: disable=protected-access
        #adal.oauth2_client._REQ_OPTION['headers']['User-Agent'] = self.user_agent

    async def user_discovery(self, username):
        """
        Discover whether this is a federated user
        """
        # Tenant specific endpoint seems to not work for this?
        authority_uri = 'https://login.microsoftonline.com/common'
        user = quote_plus(username)
        res, resdata = await self.requests_get(f"{authority_uri}/UserRealm/{user}?api-version=2.0", restype='json')
        return resdata

    #def authenticate_device_code(self):
    #    """
    #    Authenticate the end-user using device auth.
    #    """
    #
    #    authority_host_uri = self.get_authority_url()
    #
    #    context = adal.AuthenticationContext(authority_host_uri, api_version=None, proxies=self.proxies, verify_ssl=self.verify)
    #    code = context.acquire_user_code(self.resource_uri, self.client_id)
    #    await self.print(code['message'])
    #    self.tokendata = context.acquire_token_with_device_code(self.resource_uri, code, self.client_id)
    #    return self.tokendata

    #def authenticate_username_password(self):
    #    """
    #    Authenticate using user w/ username + password.
    #    This doesn't work for users or tenants that have multi-factor authentication required.
    #    """
    #    context = adal.AuthenticationContext(authority_uri, api_version=None, proxies=self.proxies, verify_ssl=self.verify)
    #    self.tokendata = context.acquire_token_with_username_password(self.resource_uri, self.username, self.password, self.client_id)
    #    return self.tokendata

    async def authenticate_username_password_native(self, client_secret=None, additionaldata=None, returnreply=False):
        """
        Authenticate using user w/ username + password.
        This doesn't work for users or tenants that have multi-factor authentication required.
        Native version without adal
        """
        authority_uri = self.get_authority_url()
        data = {
            "client_id": self.client_id,
            "grant_type": "password",
            "resource": self.resource_uri,
            "username": self.username,
            "password": self.password
        }
        if self.scope:
            data['scope'] = self.scope
        if client_secret:
            data['client_secret'] = client_secret
        if additionaldata:
            data = {**data, **additionaldata}
        await self.print(data)
        res, tokenreply = await self.requests_post(f"{authority_uri}/oauth2/token", data=data, restype='json')
        if res.status_code != 200:
            raise AuthenticationException(tokenreply)
        if returnreply:
            return tokenreply
        self.tokendata = self.tokenreply_to_tokendata(tokenreply)
        return self.tokendata

    async def authenticate_username_password_native_v2(self, client_secret=None, additionaldata=None, returnreply=False):
        """
        Authenticate using user w/ username + password.
        This doesn't work for users or tenants that have multi-factor authentication required.
        Native version without adal, for identity platform v2 endpoint
        """
        authority_uri = self.get_authority_url('organizations')
        data = {
            "client_id": self.client_id,
            "grant_type": "password",
            "scope": self.scope,
            "username": self.username,
            "password": self.password
        }
        if client_secret:
            data['client_secret'] = client_secret
        if additionaldata:
            data = {**data, **additionaldata}
        await self.print(data)
        res, tokenreply = await self.requests_post(f"{authority_uri}/oauth2/v2.0/token", data=data, restype='json')
        if res.status_code != 200:
            raise AuthenticationException(tokenreply)
        if returnreply:
            return tokenreply
        self.tokendata = self.tokenreply_to_tokendata(tokenreply)
        return self.tokendata

    #def authenticate_as_app(self):
    #    """
    #    Authenticate with an APP id + secret (password credentials assigned to serviceprinicpal)
    #    """
    #    authority_uri = self.get_authority_url()
    #
    #    context = adal.AuthenticationContext(authority_uri, api_version=None, proxies=self.proxies, verify_ssl=self.verify)
    #    self.tokendata = context.acquire_token_with_client_credentials(self.resource_uri, self.client_id, self.password)
    #    return self.tokendata

    #def authenticate_with_code(self, code, redirurl, client_secret=None):
    #    """
    #    Authenticate with a code plus optional secret in case of a non-public app (authorization grant)
    #    """
    #    authority_uri = self.get_authority_url()
    #
    #    context = adal.AuthenticationContext(authority_uri, api_version=None, proxies=self.proxies, verify_ssl=self.verify)
    #    self.tokendata = context.acquire_token_with_authorization_code(code, redirurl, self.resource_uri, self.client_id, client_secret)
    #    return self.tokendata

    #def authenticate_with_refresh(self, oldtokendata):
    #    """
    #    Authenticate with a refresh token, refreshes the refresh token
    #    and obtains an access token
    #    """
    #    authority_uri = self.get_authority_url()
    #
    #    context = adal.AuthenticationContext(authority_uri, api_version=None, proxies=self.proxies, verify_ssl=self.verify)
    #    newtokendata = context.acquire_token_with_refresh_token(oldtokendata['refreshToken'], self.client_id, self.resource_uri)
    #    # Overwrite fields
    #    for ikey, ivalue in newtokendata.items():
    #        self.tokendata[ikey] = ivalue
    #    access_token = newtokendata['accessToken']
    #    tokens = access_token.split('.')
    #    inputdata = json.loads(base64.b64decode(tokens[1]+('='*(len(tokens[1])%4))))
    #    self.tokendata['_clientId'] = self.client_id
    #    self.tokendata['tenantId'] = inputdata['tid']
    #    return self.tokendata

    async def authenticate_with_refresh_native(self, refresh_token, client_secret=None, additionaldata=None, returnreply=False):
        """
        Authenticate with a refresh token plus optional secret in case of a non-public app (authorization grant)
        Native ROADlib implementation without adal requirement
        """
        authority_uri = self.get_authority_url()
        data = {
            "client_id": self.client_id,
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "resource": self.resource_uri,
        }
        if client_secret:
            data['client_secret'] = client_secret
        if additionaldata:
            data = {**data, **additionaldata}
        res, tokenreply = await self.requests_post(f"{authority_uri}/oauth2/token", data=data, restype='json')
        if res.status_code != 200:
            raise AuthenticationException(res.text)
        if returnreply:
            return tokenreply
        access_token = tokenreply['access_token']
        tokens = access_token.split('.')
        self.tokendata = self.tokenreply_to_tokendata(tokenreply)
        return self.tokendata

    async def authenticate_with_refresh_native_v2(self, refresh_token, client_secret=None, additionaldata=None, returnreply=False):
        """
        Authenticate with a refresh token plus optional secret in case of a non-public app (authorization grant)
        Native ROADlib implementation without adal requirement
        This function calls identity platform v2 and thus requires a scope instead of resource
        """
        authority_uri = self.get_authority_url()
        data = {
            "client_id": self.client_id,
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "scope": self.scope,
        }
        if client_secret:
            data['client_secret'] = client_secret
        if additionaldata:
            data = {**data, **additionaldata}
        res, tokenreply = await self.requests_post(f"{authority_uri}/oauth2/v2.0/token", data=data, restype='json')
        if res.status_code != 200:
            raise AuthenticationException(res.text)
        if returnreply:
            return tokenreply
        self.tokendata = self.tokenreply_to_tokendata(tokenreply)
        return self.tokendata

    async def authenticate_with_code_native(self, code, redirurl, client_secret=None, pkce_secret=None, additionaldata=None, returnreply=False):
        """
        Authenticate with a code plus optional secret in case of a non-public app (authorization grant)
        Native ROADlib implementation without adal requirement - also supports PKCE
        """
        authority_uri = self.get_authority_url()
        data = {
            "client_id": self.client_id,
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": redirurl,
            "resource": self.resource_uri,
        }
        if client_secret:
            data['client_secret'] = client_secret
        if additionaldata:
            data = {**data, **additionaldata}
        if pkce_secret:
            raise NotImplementedError
        res, tokenreply = await self.requests_post(f"{authority_uri}/oauth2/token", data=data, restype='json')
        if res.status_code != 200:
            raise AuthenticationException(res.text)
        if returnreply:
            return tokenreply
        self.tokendata = self.tokenreply_to_tokendata(tokenreply)
        return self.tokendata

    async def authenticate_with_code_native_v2(self, code, redirurl, client_secret=None, pkce_secret=None, additionaldata=None, returnreply=False):
        """
        Authenticate with a code plus optional secret in case of a non-public app (authorization grant)
        Native ROADlib implementation without adal requirement - also supports PKCE
        This function calls identity platform v2 and thus requires a scope instead of resource
        """
        authority_uri = self.get_authority_url()
        data = {
            "client_id": self.client_id,
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": redirurl,
            "scope": self.scope,
        }
        if client_secret:
            data['client_secret'] = client_secret
        if additionaldata:
            data = {**data, **additionaldata}
        if pkce_secret:
            raise NotImplementedError
        res, tokenreply = await self.requests_post(f"{authority_uri}/oauth2/v2.0/token", data=data, restype='json')
        if res.status_code != 200:
            raise AuthenticationException(res.text)
        if returnreply:
            return tokenreply
        self.tokendata = self.tokenreply_to_tokendata(tokenreply)
        return self.tokendata

    async def authenticate_with_code_encrypted(self, code, sessionkey, redirurl):
        '''
        Encrypted code redemption. Like normal code flow but requires
        session key to decrypt response.
        '''
        authority_uri = self.get_authority_url()
        data = {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": redirurl,
            "client_id": self.client_id,
            "client_info":1,
            "windows_api_version":"2.0"
        }
        res, prtdata = await self.requests_post(f"{authority_uri}/oauth2/token", data=data, restype='text')
        if res.status_code != 200:
            raise AuthenticationException(res.text)
        data = self.decrypt_auth_response(prtdata, sessionkey, asjson=True)
        return data

    async def authenticate_with_saml_native(self, saml_token, additionaldata=None, returnreply=False):
        """
        Authenticate with a SAML token from the Federation Server
        Native ROADlib implementation without adal requirement
        """
        authority_uri = self.get_authority_url()
        data = {
            "client_id": self.client_id,
            "grant_type": "urn:ietf:params:oauth:grant-type:saml1_1-bearer",
            "assertion": base64.b64encode(saml_token.encode('utf-8')).decode('utf-8'),
            "resource": self.resource_uri,
        }
        if additionaldata:
            data = {**data, **additionaldata}
        res, tokenreply = await self.requests_post(f"{authority_uri}/oauth2/token", data=data, restype='json')
        if res.status_code != 200:
            raise AuthenticationException(res.text)
        if returnreply:
            return tokenreply
        self.tokendata = self.tokenreply_to_tokendata(tokenreply)
        return self.tokendata

    async def authenticate_with_saml_native_v2(self, saml_token, additionaldata=None, returnreply=False):
        """
        Authenticate with a SAML token from the Federation Server
        Native ROADlib implementation without adal requirement
        This function calls identity platform v2 and thus requires a scope instead of resource
        """
        authority_uri = self.get_authority_url()
        data = {
            "client_id": self.client_id,
            "grant_type": "urn:ietf:params:oauth:grant-type:saml1_1-bearer",
            "assertion": base64.b64encode(saml_token.encode('utf-8')).decode('utf-8'),
            "scope": self.scope,
        }
        if additionaldata:
            data = {**data, **additionaldata}
        res, tokenreply = await self.requests_post(f"{authority_uri}/oauth2/v2.0/token", data=data, restype='json')
        if res.status_code != 200:
            raise AuthenticationException(res.text)
        if returnreply:
            return tokenreply
        self.tokendata = self.tokenreply_to_tokendata(tokenreply)
        return self.tokendata

    async def get_desktopsso_token(self, username=None, password=None, krbtoken=None):
        '''
        Get desktop SSO token either with plain username and password, or with a Kerberos auth token
        '''
        if username and password:
            rbody = DSSO_BODY_USERPASS.format(username=xml_escape(username), password=xml_escape(password), tenant=self.tenant)
            headers = {
                'Content-Type':'application/soap+xml; charset=utf-8',
                'SOAPAction': 'http://schemas.xmlsoap.org/ws/2005/02/trust/RST/Issue',
            }
            res, rescontent = await self.requests_post(f'https://autologon.microsoftazuread-sso.com/{self.tenant}/winauth/trust/2005/usernamemixed?client-request-id=19ac39db-81d2-4713-8046-b0b7240592be', headers=headers, data=rbody, restype='text')
            tree = ET.fromstring(rescontent)
            els = tree.findall('.//DesktopSsoToken')
            if len(els) > 0:
                token = els[0].text
                return token
            else:
                # Try finding error
                elres = tree.iter('{http://schemas.microsoft.com/Passport/SoapServices/SOAPFault}text')
                if elres:
                    errtext = next(elres)
                    raise AuthenticationException(errtext.text)
                else:
                    raise AuthenticationException(parseString(rescontent).toprettyxml(indent='  '))
        elif krbtoken:
            rbody = DSSO_BODY_KERBEROS.format(tenant=self.tenant)
            headers = {
                'Content-Type':'application/soap+xml; charset=utf-8',
                'SOAPAction': 'http://schemas.xmlsoap.org/ws/2005/02/trust/RST/Issue',
                'Authorization': f'Negotiate {krbtoken}'
            }
            res, rescontent = await self.requests_post(f'https://autologon.microsoftazuread-sso.com/{self.tenant}/winauth/trust/2005/windowstransport?client-request-id=19ac39db-81d2-4713-8046-b0b7240592be', headers=headers, data=rbody, restype='text')
            tree = ET.fromstring(rescontent)
            els = tree.findall('.//DesktopSsoToken')
            if len(els) > 0:
                token = els[0].text
                return token
            else:
                await self.print(parseString(rescontent).toprettyxml(indent='  '))
                return False
        else:
            return False

    async def authenticate_with_desktopsso_token(self, dssotoken, returnreply=False, additionaldata=None):
        '''
        Authenticate with Desktop SSO token
        '''
        headers = {
            'x-client-SKU': 'PCL.Desktop',
            'x-client-Ver': '3.19.7.16602',
            'x-client-CPU': 'x64',
            'x-client-OS': 'Microsoft Windows NT 10.0.18363.0',
            'x-ms-PKeyAuth': '1.0',
            'client-request-id': '19ac39db-81d2-4713-8046-b0b7240592be',
            'return-client-request-id': 'true',
        }
        claim = base64.b64encode('<saml:Assertion xmlns:saml="urn:oasis:names:tc:SAML:1.0:assertion"><DesktopSsoToken>{0}</DesktopSsoToken></saml:Assertion>'.format(dssotoken).encode('utf-8')).decode('utf-8')
        data = {
            'resource': self.resource_uri,
            'client_id': self.client_id,
            'grant_type': 'urn:ietf:params:oauth:grant-type:saml1_1-bearer',
            'assertion': claim,
        }
        authority_uri = self.get_authority_url()
        if additionaldata:
            data = {**data, **additionaldata}
        res = await self.requests_post(f"{authority_uri}/oauth2/token", headers=headers, data=data)
        if res.status_code != 200:
            raise AuthenticationException(res.text)
        tokenreply = res.json()
        if returnreply:
            return tokenreply
        self.tokendata = self.tokenreply_to_tokendata(tokenreply)
        return self.tokendata

    async def get_bulk_enrollment_token(self, access_token):
        body = {
            "pid": str(uuid.uuid4()),
            "name": "bulktoken",
            "exp": (datetime.datetime.now() + datetime.timedelta(days=90)).strftime('%Y-%m-%d %H:%M:%S')
        }
        headers = {
            'Authorization': f'Bearer {access_token}'
        }
        url = 'https://login.microsoftonline.com/webapp/bulkaadjtoken/begin'
        res, data = await self.requests_post(url, headers=headers, data=body, reqtype='json', restype='json')
        state = data.get('state')
        if not state:
            await self.print(f'No state returned. Server said: {data}')
            return False

        if state == 'CompleteError':
            await self.print(f'Error occurred: {data["resultData"]}')
            return False

        flowtoken = data.get('flowToken')
        if not flowtoken:
            await self.print(f'Error. No flow token found. Data: {data}')
            return False
        await self.print('Got flow token, polling for token creation')
        url = 'https://login.microsoftonline.com/webapp/bulkaadjtoken/poll'
        while True:
            res, data = await self.requests_get(f"{url}/?flowtoken={flowtoken}",  headers=headers, restype='json')
            state = data.get('state')
            if not state:
                await self.print(f'No state returned. Server said: {data}')
                return False
            if state == 'CompleteError':
                await self.print(f'Error occurred: {data["resultData"]}')
                return False
            if state == 'CompleteSuccess':
                tokenresult = json.loads(data['resultData'])
                # The function below needs one so lets supply it
                tokenresult['access_token'] = tokenresult['id_token']
                self.tokendata = self.tokenreply_to_tokendata(tokenresult, client_id='b90d5b8f-5503-4153-b545-b31cecfaece2')
                return self.tokendata
            time.sleep(1.0)

    def build_auth_url(self, redirurl, response_type, scope=None, state=None):
        '''
        Build authorize URL. Can be v2 by specifying scope, otherwise defaults
        to v1 with resource
        '''
        urlt_v2 = 'https://login.microsoftonline.com/{3}/oauth2/v2.0/authorize?response_type={4}&client_id={0}&scope={2}&redirect_uri={1}&state={5}'
        urlt_v1 = 'https://login.microsoftonline.com/{3}/oauth2/authorize?response_type={4}&client_id={0}&resource={2}&redirect_uri={1}&state={5}'
        if not state:
            state = str(uuid.uuid4())
        if not self.tenant:
            tenant = 'common'
        else:
            tenant = self.tenant
        if scope:
            # v2
            return urlt_v2.format(
                quote_plus(self.client_id),
                quote_plus(redirurl),
                quote_plus(scope),
                quote_plus(tenant),
                quote_plus(response_type),
                quote_plus(state)
            )
        # Else default to v1 identity endpoint
        return urlt_v1.format(
            quote_plus(self.client_id),
            quote_plus(redirurl),
            quote_plus(self.resource_uri),
            quote_plus(tenant),
            quote_plus(response_type),
            quote_plus(state)
        )

    def create_prt_cookie_kdf_ver_2(self, prt, sessionkey, nonce=None):
        """
        KDF version 2 cookie construction
        """
        context = os.urandom(24)
        headers = {
            'ctx': base64.b64encode(context).decode('utf-8'), #.rstrip('=')
            'kdf_ver': 2
        }
        # Nonce should be requested by calling function, otherwise
        # old time based model is used
        if nonce:
            payload = {
                "refresh_token": prt,
                "is_primary": "true",
                "request_nonce": nonce
            }
        else:
            payload = {
                "refresh_token": prt,
                "is_primary": "true",
                "iat": str(int(time.time()))
            }
        # Sign with random key just to get jwt body in right encoding
        tempjwt = jwt.encode(payload, os.urandom(32), algorithm='HS256', headers=headers)
        jbody = tempjwt.split('.')[1]
        jwtbody = base64.b64decode(jbody+('='*(len(jbody)%4)))

        # Now calculate the derived key based on random context plus jwt body
        _, derived_key = self.calculate_derived_key_v2(sessionkey, context, jwtbody)
        cookie = jwt.encode(payload, derived_key, algorithm='HS256', headers=headers)
        return cookie

    async def authenticate_with_prt_v2(self, prt, sessionkey):
        """
        KDF version 2 PRT auth
        """
        nonce = await self.get_prt_cookie_nonce()
        if not nonce:
            return False

        cookie = self.create_prt_cookie_kdf_ver_2(prt, sessionkey, nonce)
        return await self.authenticate_with_prt_cookie(cookie)

    async def authenticate_with_prt(self, prt, context, derived_key=None, sessionkey=None):
        """
        Authenticate with a PRT and given context/derived key
        Uses KDF version 1 (legacy)
        """
        # If raw key specified, use that
        if not derived_key and sessionkey:
            context, derived_key = self.calculate_derived_key(sessionkey, context)

        headers = {
            'ctx': base64.b64encode(context).decode('utf-8'),
        }
        nonce = await self.get_prt_cookie_nonce()
        if not nonce:
            return False
        payload = {
            "refresh_token": prt,
            "is_primary": "true",
            "request_nonce": nonce
        }
        cookie = jwt.encode(payload, derived_key, algorithm='HS256', headers=headers)
        return await self.authenticate_with_prt_cookie(cookie)

    def calculate_derived_key_v2(self, sessionkey, context, jwtbody):
        """
        Derived key calculation v2, which uses the JWT body
        """
        digest = hashes.Hash(hashes.SHA256())
        digest.update(context)
        digest.update(jwtbody)
        kdfcontext = digest.finalize()
        # From here on identical to v1
        return self.calculate_derived_key(sessionkey, kdfcontext)

    def calculate_derived_key(self, sessionkey, context=None):
        """
        Calculate the derived key given a session key and optional context using KBKDFHMAC
        """
        label = b"AzureAD-SecureConversation"
        if not context:
            context = os.urandom(24)
        backend = default_backend()
        kdf = KBKDFHMAC(
            algorithm=hashes.SHA256(),
            mode=Mode.CounterMode,
            length=32,
            rlen=4,
            llen=4,
            location=CounterLocation.BeforeFixed,
            label=label,
            context=context,
            fixed=None,
            backend=backend
        )
        derived_key = kdf.derive(sessionkey)
        return context, derived_key

    def decrypt_auth_response(self, responsedata, sessionkey, asjson=False):
        """
        Decrypt an encrypted authentication response, which is a JWE
        encrypted using the sessionkey
        """
        if responsedata[:2] == '{"':
            # This doesn't appear encrypted
            if asjson:
                return json.loads(responsedata)
            return responsedata
        dataparts = responsedata.split('.')

        headers = json.loads(get_data(dataparts[0]))
        _, derived_key = self.calculate_derived_key(sessionkey, base64.b64decode(headers['ctx']))
        data = dataparts[3]
        iv = dataparts[2]
        authtag = dataparts[4]
        if len(get_data(iv)) == 12:
            # This appears to be actual AES GCM
            aesgcm = AESGCM(derived_key)
            # JWE header is used as additional data
            # Totally legit source: https://github.com/AzureAD/microsoft-authentication-library-common-for-objc/compare/dev...kedicl/swift/addframework#diff-ec15357c1b0dba2f2304f64750e5126ec910156f09c0f75eba0bb22cb83ada6dR46
            # Also hinted at in RFC examples https://www.rfc-editor.org/rfc/rfc7516.txt
            depadded_data = aesgcm.decrypt(get_data(iv), get_data(data) + get_data(authtag), dataparts[0].encode('utf-8'))
        else:
            cipher = Cipher(algorithms.AES(derived_key), modes.CBC(get_data(iv)))
            decryptor = cipher.decryptor()
            decrypted_data = decryptor.update(get_data(data)) + decryptor.finalize()
            unpadder = padding.PKCS7(128).unpadder()
            depadded_data = unpadder.update(decrypted_data) + unpadder.finalize()
        if asjson:
            jdata = json.loads(depadded_data)
            return jdata
        else:
            return depadded_data

    async def get_srv_challenge(self):
        """
        Request server challenge (nonce) to use with a PRT
        """
        data = {'grant_type':'srv_challenge'}
        res, data = await self.requests_post('https://login.microsoftonline.com/common/oauth2/token', data=data, restype='json')
        return data

    async def get_prt_cookie_nonce(self):
        """
        Request a nonce to sign in with. This nonce is taken from the sign-in page, which
        is how Chrome processes it, but it could probably also be obtained using the much
        simpler request from the get_srv_challenge function.
        """
        #ses = requests.session()
        proxies = self.proxies
        verify = self.verify
        if self.user_agent:
            headers = {'User-Agent': self.user_agent}
            #ses.headers = headers
        params = {
            'resource': str(self.resource_uri),
            'client_id': str(self.client_id),
            'response_type': 'code',
            'haschrome': '1',
            'redirect_uri': 'https://login.microsoftonline.com/common/oauth2/nativeclient',
            'client-request-id': str(uuid.uuid4()),
            'x-client-SKU': 'PCL.Desktop',
            'x-client-Ver': '3.19.7.16602',
            'x-client-CPU': 'x64',
            'x-client-OS': 'Microsoft Windows NT 10.0.19569.0',
            'site_id': '501358',
            'mscrid': str(uuid.uuid4())
        }
        headers = {
            'User-Agent': 'Mozilla/4.0 (compatible; MSIE 7.0; Windows NT 10.0; Win64; x64; Trident/7.0; .NET4.0C; .NET4.0E)',
            'UA-CPU': 'AMD64',
        }
        base_url = 'https://login.microsoftonline.com/Common/oauth2/authorize/'
        query_string = urlencode(params)
        full_url = base_url + '?' + query_string
        res, resdata = await self.requests_get(full_url, headers=headers, follow_redirects=False, restype='content')
        #res = ses.get('https://login.microsoftonline.com/Common/oauth2/authorize', params=params, headers=headers, follow_redirects=False)
        if self.debug:
            with open('roadtools.debug.html','w') as outfile:
                outfile.write(str(res.headers))
                outfile.write('------\n\n\n-----')
                outfile.write(resdata.decode('utf-8'))
        if res.status == 302 and res.headers['Location']:
            ups = urlparse(res.headers['Location'])
            qdata = parse_qs(ups.query)
            if not 'sso_nonce' in qdata:
                await self.print('No nonce found in redirect!')
                return
            return qdata['sso_nonce'][0]
        else:
            # Try to find SSO nonce in json config
            startpos = resdata.find(b'$Config=')
            stoppos = resdata.find(b'//]]></script>')
            if startpos == -1 or stoppos == -1:
                await self.print('No redirect or nonce config was returned!')
                return
            else:
                jsonbytes = resdata[startpos+8:stoppos-2]
                try:
                    jdata = json.loads(jsonbytes)
                except json.decoder.JSONDecodeError:
                    await self.print('Failed to parse config JSON')
                try:
                    nonce = jdata['bsso']['nonce']
                except KeyError:
                    await self.print('No nonce found in browser config')
                    return
                return nonce


    async def authenticate_with_prt_cookie(self, cookie, context=None, derived_key=None, verify_only=False, sessionkey=None, redirurl=None, return_code=False):
        """
        Authenticate with a PRT cookie, optionally re-signing the cookie if a key is given
        """
        # Load cookie
        jdata = jwt.decode(cookie, options={"verify_signature":False}, algorithms=['HS256'])
        # Does it have a nonce?
        if not 'request_nonce' in jdata:
            nonce = await self.get_prt_cookie_nonce()
            if not nonce:
                return False
            await self.print('Requested nonce from server to use with ROADtoken: %s' % nonce)
            if not derived_key and not sessionkey:
                return False
        else:
            nonce = jdata['request_nonce']

        # If raw key specified, use that
        if not derived_key and sessionkey:
            context, derived_key = self.calculate_derived_key(sessionkey, context)

        # If a derived key was specified, we use that
        if derived_key:
            sdata = derived_key
            headers = jwt.get_unverified_header(cookie)
            if context is None or verify_only:
                # Verify JWT
                try:
                    jdata = jwt.decode(cookie, sdata, algorithms=['HS256'])
                except jwt.exceptions.InvalidSignatureError:
                    await self.print('Signature invalid with given derived key')
                    return False
                if verify_only:
                    await self.print('PRT verified with given derived key!')
                    return False
            else:
                # Don't verify JWT, just load it
                jdata = jwt.decode(cookie, sdata, options={"verify_signature":False}, algorithms=['HS256'])
            # Since a derived key was specified, we get a new nonce
            nonce = await self.get_prt_cookie_nonce()
            jdata['request_nonce'] = nonce
            if context:
                # Resign with custom context, should be in base64
                newheaders = {
                    'ctx': base64.b64encode(context).decode('utf-8') #.rstrip('=')
                }
                cookie = jwt.encode(jdata, sdata, algorithm='HS256', headers=newheaders)
                await self.print('Re-signed PRT cookie using custom context')
            else:
                newheaders = {
                    'ctx': headers['ctx']
                }
                cookie = jwt.encode(jdata, sdata, algorithm='HS256', headers=newheaders)
                await self.print('Re-signed PRT cookie using derived key')

        #ses = requests.session()
        proxies = self.proxies
        verify = self.verify
        if self.user_agent:
            headers = {'User-Agent': self.user_agent}
        
        authority_uri = self.get_authority_url()
        params = {
            'client_id': str(self.client_id),
            'response_type': 'code',
            'haschrome': '1',
            'redirect_uri': 'https://login.microsoftonline.com/common/oauth2/nativeclient',
            'client-request-id': str(uuid.uuid4()),
            'x-client-SKU': 'PCL.Desktop',
            'x-client-Ver': '3.19.7.16602',
            'x-client-CPU': 'x64',
            'x-client-OS': 'Microsoft Windows NT 10.0.19569.0',
            'site_id': '501358',
            'sso_nonce': nonce,
            'mscrid': str(uuid.uuid4())
        }
        if self.scope:
            # Switch to identity platform v2 endpoint
            params['scope'] = self.scope
            url = f'{authority_uri}/oauth2/v2.0/authorize'
            coderedeemfunc = self.authenticate_with_code_native_v2
        else:
            params['resource'] = self.resource_uri
            url = f'{authority_uri}/oauth2/authorize'
            coderedeemfunc = self.authenticate_with_code_native

        headers = {
            'UA-CPU': 'AMD64',
        }
        if not self.user_agent:
            # Add proper user agent if we don't have one yet
            headers['User-Agent'] = 'Mozilla/4.0 (compatible; MSIE 7.0; Windows NT 10.0; Win64; x64; Trident/7.0; .NET4.0C; .NET4.0E)'

        cookies = {
            'x-ms-RefreshTokenCredential': cookie
        }
        if redirurl:
            params['redirect_uri'] = redirurl

        full_url = url + '?' + urlencode(params)
        res, resdata = await self.requests_get(full_url, headers=headers, cookies=cookies, follow_redirects=False, restype='content')
        #res = ses.get(url, params=params, headers=headers, cookies=cookies, follow_redirects=False)
        if res.status == 302 and params['redirect_uri'].lower() in res.headers['Location'].lower():
            ups = urlparse(res.headers['Location'])
            qdata = parse_qs(ups.query)
            # Return code if requested, otherwise redeem it
            if return_code:
                return qdata['code'][0]
            return coderedeemfunc(qdata['code'][0], params['redirect_uri'])
        if res.status == 302 and 'sso_nonce' in res.headers['Location'].lower():
            ups = urlparse(res.headers['Location'])
            qdata = parse_qs(ups.query)
            if 'sso_nonce' in qdata:
                nonce = qdata['sso_nonce'][0]
                await self.print(f'Redirected with new nonce. Old nonce may be expired. New nonce: {nonce}')
        if self.debug:
            with open('roadtools.debug.html','w') as outfile:
                outfile.write(str(res.headers))
                outfile.write('------\n\n\n-----')
                outfile.write(resdata.decode('utf-8'))

        # Try to find SSO nonce in json config
        startpos = resdata.find(b'$Config=')
        stoppos = resdata.find(b'//]]></script>')
        if startpos != -1 and stoppos != -1:
            jsonbytes = resdata[startpos+8:stoppos-2]
            try:
                jdata = json.loads(jsonbytes)
                try:
                    error = jdata['strMainMessage']
                    await self.print(f'Error from STS: {error}')
                    error = jdata['strAdditionalMessage']
                    await self.print(f'Additional info: {error}')
                    error = jdata['strServiceExceptionMessage']
                    await self.print(f'Exception: {error}')
                except KeyError:
                    pass
            except json.decoder.JSONDecodeError:
                pass
        await self.print('No authentication code was returned, make sure the PRT cookie is valid')
        await self.print('It is also possible that the sign-in was blocked by Conditional Access policies')
        return False

    @staticmethod
    def get_sub_argparse(auth_parser, for_rr=False):
        """
        Get an argparse subparser for authentication
        """
        auth_parser.add_argument('-u',
                                 '--username',
                                 action='store',
                                 help='Username for authentication')
        auth_parser.add_argument('-p',
                                 '--password',
                                 action='store',
                                 help='Password (leave empty to prompt)')
        auth_parser.add_argument('-t',
                                 '--tenant',
                                 action='store',
                                 help='Tenant ID to auth to (leave blank for default tenant for account)')
        if for_rr:
            helptext = 'Client ID to use when authenticating. (Must be a public client from Microsoft with user_impersonation permissions!). Default: Azure AD PowerShell module App ID'
        else:
            helptext = 'Client ID to use when authenticating. (Must have the required OAuth2 permissions or Application Role for what you want to achieve)'
        auth_parser.add_argument('-c',
                                 '--client',
                                 action='store',
                                 help=helptext,
                                 default='1b730954-1685-4b74-9bfd-dac224a7b894')
        auth_parser.add_argument('-r',
                                 '--resource',
                                 action='store',
                                 help='Resource to authenticate to (Default: https://graph.windows.net)',
                                 default='https://graph.windows.net')
        auth_parser.add_argument('-s',
                                 '--scope',
                                 action='store',
                                 help='Scope to request when authenticating. Not supported in all flows yet. Overrides resource if specified')
        auth_parser.add_argument('--as-app',
                                 action='store_true',
                                 help='Authenticate as App (requires password and client ID set)')
        auth_parser.add_argument('--device-code',
                                 action='store_true',
                                 help='Authenticate using a device code')
        auth_parser.add_argument('--access-token',
                                 action='store',
                                 help='Access token (JWT)')
        auth_parser.add_argument('--refresh-token',
                                 action='store',
                                 help='Refresh token (or the word "file" to read it from .roadtools_auth)')
        auth_parser.add_argument('--saml-token',
                                 action='store',
                                 help='SAML token from Federation Server')
        auth_parser.add_argument('--prt-cookie',
                                 action='store',
                                 help='Primary Refresh Token cookie from ROADtoken (JWT)')
        auth_parser.add_argument('--prt-init',
                                 action='store_true',
                                 help='Initialize Primary Refresh Token authentication flow (get nonce)')
        auth_parser.add_argument('--prt',
                                 action='store',
                                 help='Primary Refresh Token')
        auth_parser.add_argument('--derived-key',
                                 action='store',
                                 help='Derived key used to re-sign the PRT cookie (as hex key)')
        auth_parser.add_argument('--prt-context',
                                 action='store',
                                 help='Primary Refresh Token context for the derived key (as hex key)')
        auth_parser.add_argument('--prt-sessionkey',
                                 action='store',
                                 help='Primary Refresh Token session key (as hex key)')
        auth_parser.add_argument('--prt-verify',
                                 action='store_true',
                                 help='Verify the Primary Refresh Token and exit')
        auth_parser.add_argument('--kdf-v1',
                                 action='store_true',
                                 help='Use the older KDF version for PRT auth, may not work with PRTs from modern OSs')
        auth_parser.add_argument('-ua',
                                 '--user-agent',
                                 action='store',
                                 help='User agent or UA alias to use when requesting tokens (default: python-requests/version)')
        auth_parser.add_argument('-f',
                                 '--tokenfile',
                                 action='store',
                                 help='File to save the tokens to (default: .roadtools_auth)',
                                 default='.roadtools_auth')
        auth_parser.add_argument('--tokens-stdout',
                                 action='store_true',
                                 help='Do not store tokens on disk, pipe to stdout instead')
        auth_parser.add_argument('--debug',
                                 action='store_true',
                                 help='Enable debug logging to disk')
        return auth_parser

    @staticmethod
    def ensure_binary_derivedkey(derived_key):
        if not derived_key:
            return None
        secret = derived_key.replace(' ','')
        sdata = binascii.unhexlify(secret)
        return sdata

    @staticmethod
    def ensure_binary_sessionkey(sessionkey):
        if not sessionkey:
            return None
        if len(sessionkey) == 44:
            keybytes = base64.b64decode(sessionkey)
        else:
            sessionkey = sessionkey.replace(' ','')
            keybytes = binascii.unhexlify(sessionkey)
        return keybytes

    @staticmethod
    def ensure_binary_context(context):
        if not context:
            return None
        return binascii.unhexlify(context)

    @staticmethod
    def ensure_plain_prt(prt):
        if not prt:
            return None
        # May be base64 encoded
        if not '.' in prt:
            prt = base64.b64decode(prt+('='*(len(prt)%4))).decode('utf-8')
        return prt

    @staticmethod
    def parse_accesstoken(token):
        tokenparts = token.split('.')
        tokendata = json.loads(get_data(tokenparts[1]))
        tokenobject = {
            'accessToken': token,
            'tokenType': 'Bearer',
            'expiresOn': datetime.datetime.fromtimestamp(tokendata['exp']).strftime('%Y-%m-%d %H:%M:%S'),
            'tenantId': tokendata.get('tid'),
            '_clientId': tokendata.get('appid')
        }
        return tokenobject, tokendata

    @staticmethod
    def tokenreply_to_tokendata(tokenreply, client_id=None):
        """
        Convert /token reply from Azure to ADAL compatible object
        """
        tokenobject = {
            'tokenType': tokenreply['token_type'],
        }
        try:
            tokenobject['expiresOn'] = datetime.datetime.fromtimestamp(int(tokenreply['expires_on'])).strftime('%Y-%m-%d %H:%M:%S')
        except KeyError:
            tokenobject['expiresOn'] = (datetime.datetime.now() + datetime.timedelta(seconds=int(tokenreply['expires_in']))).strftime('%Y-%m-%d %H:%M:%S')

        tokenparts = tokenreply['access_token'].split('.')
        inputdata = json.loads(base64.b64decode(tokenparts[1]+('='*(len(tokenparts[1])%4))))
        try:
            tokenobject['tenantId'] = inputdata['tid']
        except KeyError:
            pass
        if client_id:
            tokenobject['_clientId'] = client_id
        else:
            try:
                tokenobject['_clientId'] = inputdata['appid']
            except KeyError:
                pass
        translate_map = {
            'access_token': 'accessToken',
            'refresh_token': 'refreshToken',
            'id_token': 'idToken',
            'token_type': 'tokenType'
        }
        for newname, oldname in translate_map.items():
            if newname in tokenreply:
                tokenobject[oldname] = tokenreply[newname]
        return tokenobject

    @staticmethod
    async def parse_compact_jwe(jwe, verbose=False, decode_header=True):
        """
        Parse compact JWE according to
        https://datatracker.ietf.org/doc/html/rfc7516#section-3.1
        """
        dataparts = jwe.split('.')
        header, enc_key, iv, ciphertext, auth_tag = dataparts
        parsed_header = json.loads(get_data(header))
        if verbose:
            await printhook("Header (decoded):")
            await printhook(json.dumps(parsed_header, sort_keys=True, indent=4))
            await printhook("Encrypted key:")
            await printhook(enc_key)
            await printhook('IV:')
            await printhook(iv)
            await printhook("Ciphertext:")
            await printhook(ciphertext)
            await printhook("Auth tag:")
            await printhook(auth_tag)
        if decode_header:
            return parsed_header, enc_key, iv, ciphertext, auth_tag
        return header, enc_key, iv, ciphertext, auth_tag

    @staticmethod
    def parse_jwt(jwtoken):
        """
        Simple JWT parsing function
        returns header and body as dict and signature as bytes
        """
        dataparts = jwtoken.split('.')
        header = json.loads(get_data(dataparts[0]))
        body = json.loads(get_data(dataparts[1]))
        signature = get_data(dataparts[2])
        return header, body, signature

    @staticmethod
    def lookup_resource_uri(uri):
        """
        Translate resource URI aliases
        """
        try:
            resolved = WELLKNOWN_RESOURCES[uri.lower()]
            return resolved
        except KeyError:
            return uri

    @staticmethod
    def lookup_client_id(clid):
        """
        Translate client ID aliases
        """
        try:
            resolved = WELLKNOWN_CLIENTS[clid.lower()]
            return resolved
        except KeyError:
            return clid

    @staticmethod
    def lookup_user_agent(useragent):
        """
        Translate user agents aliases
        """
        if useragent is None:
            return useragent
        #if useragent.upper() == 'EMPTY':
        #    return SKIP_HEADER
        try:
            resolved = WELLKNOWN_USER_AGENTS[useragent.lower()]
            return resolved
        except KeyError:
            return useragent

    async def requests_get(self, url, headers=None, data=None, restype = 'json', reqtype=None, follow_redirects=True):
        '''
        Wrapper around requests.get to set all the options uniformly
        '''
        #kwargs['proxies'] = self.proxies
        #kwargs['verify'] = self.verify
        #if self.user_agent:
        #    headers = kwargs.get('headers',{})
        #    headers['User-Agent'] = self.user_agent
        #    kwargs['headers'] = headers
        #return requests.get(*args, timeout=30.0, **kwargs)

        async with httpx.AsyncClient(transport=self.httptransport, auth = self.httpauth) as session:
            response = await session.get(url, headers=headers, data=data, follow_redirects=follow_redirects)
            if restype == 'json':
                resdata = await response.json()
            elif restype == 'content':
                resdata = await response.read()
            elif restype == 'text':
                resdata = await response.text()
            else:
                resdata = await response.text()
        print(resdata)
        
        return response, resdata

    async def requests_post(self, url, headers=None, data=None, restype = 'json', reqtype=None, follow_redirects=True):
        '''
        Wrapper around requests.post to set all the options uniformly
        '''
        #kwargs['proxies'] = self.proxies
        #kwargs['verify'] = self.verify
        #if self.user_agent:
        #    headers = kwargs.get('headers',{})
        #    headers['User-Agent'] = self.user_agent
        #    kwargs['headers'] = headers
        #return requests.post(*args, timeout=30.0, **kwargs)

        async with httpx.AsyncClient(transport=self.httptransport, auth = self.httpauth, follow_redirects=follow_redirects) as session:
            if reqtype == 'json':
                response = await session.post(url, headers=headers, data=data)
                if restype == 'json':
                    resdata = response.json()
                elif restype == 'content':
                    resdata = response.read()
                elif restype == 'text':
                    resdata = response.text()
                else:
                    resdata = response.text()

            else:
                response = await session.post(url, headers=headers, data=data)
                if restype == 'json':
                    resdata = response.json()
                elif restype == 'content':
                    resdata = response.content()
                elif restype == 'text':
                    resdata = response.text
                else:
                    resdata = response.text()

        return response, resdata

    def parse_args(self, args):
        self.username = args.username
        self.password = args.password
        self.tenant = args.tenant
        self.set_client_id(args.client)
        self.access_token = args.access_token
        self.refresh_token = args.refresh_token
        self.saml_token = args.saml_token
        self.outfile = args.tokenfile
        self.debug = args.debug
        self.set_resource_uri(args.resource)
        self.scope = args.scope
        #self.set_user_agent(args.user_agent)

        if not self.username is None and self.password is None:
            self.password = getpass.getpass()

    async def get_tokens(self, args):
        """
        Get tokens based on the arguments specified.
        Expects args to be generated from get_sub_argparse
        """
        try:
            if self.tokendata:
                return self.tokendata
            if self.refresh_token and not self.access_token:
                if self.refresh_token == 'file':
                    with codecs.open(args.tokenfile, 'r', 'utf-8') as infile:
                        token_data = json.load(infile)
                else:
                    token_data = {'refreshToken': self.refresh_token}
                if self.scope:
                    return await self.authenticate_with_refresh_native_v2(token_data['refreshToken'], client_secret=self.password)
                return await self.authenticate_with_refresh_native(token_data['refreshToken'], client_secret=self.password)
            if self.access_token and not self.refresh_token:
                self.tokendata, _ = self.parse_accesstoken(self.access_token)
                return self.tokendata
            if self.username and self.password:
                #udres = await self.user_discovery(self.username)
                #if udres['NameSpaceType'] == 'Federated':
                    # Fall back to adal until we have support for this
                    #raise Exception('Federated auth not supported yet. ADAL not available here')
                # Use native implementation
                if self.scope:
                    return await self.authenticate_username_password_native_v2()
                return await self.authenticate_username_password_native()
            if self.saml_token:
                if self.saml_token.lower() == 'stdin':
                    samltoken = sys.stdin.read()
                else:
                    samltoken = self.saml_token
                if self.scope:
                    # Use v2 endpoint if we have a scope
                    return await self.authenticate_with_saml_native_v2(samltoken)
                else:
                    return await self.authenticate_with_saml_native(samltoken)
            if args.as_app and self.password:
                raise Exception('App auth not supported yet. ADAL not available here')
                #return self.authenticate_as_app()
            if args.device_code:
                raise Exception('Device code auth not supported yet. ADAL not available here')
                #return self.authenticate_device_code()
            if args.prt_init:
                nonce = await self.get_prt_cookie_nonce()
                if nonce:
                    await self.print(f'Requested nonce from server to use with ROADtoken: {nonce}')
                return False
            if args.prt_cookie:
                derived_key = self.ensure_binary_derivedkey(args.derived_key)
                context = self.ensure_binary_context(args.prt_context)
                sessionkey = self.ensure_binary_sessionkey(args.prt_sessionkey)
                return await self.authenticate_with_prt_cookie(args.prt_cookie, context, derived_key, args.prt_verify, sessionkey)
            if args.prt and args.prt_context and args.derived_key:
                derived_key = self.ensure_binary_derivedkey(args.derived_key)
                context = self.ensure_binary_context(args.prt_context)
                prt = self.ensure_plain_prt(args.prt)
                return await self.authenticate_with_prt(prt, context, derived_key=derived_key)
            if args.prt and args.prt_sessionkey:
                prt = self.ensure_plain_prt(args.prt)
                sessionkey = self.ensure_binary_sessionkey(args.prt_sessionkey)
                if args.kdf_v1:
                    return await self.authenticate_with_prt(prt, None, sessionkey=sessionkey)
                else:
                    return await self.authenticate_with_prt_v2(prt, sessionkey)

        #except adal.adal_error.AdalError as ex:
        #    try:
        #        await self.print(f"Error during authentication: {ex.error_response['error_description']}")
        #    except TypeError:
        #        # Not all errors are objects
        #        await self.print(str(ex))
        #    sys.exit(1)
        except AuthenticationException as ex:
            print(ex)
            try:
                error_data = json.loads(str(ex))
                await self.print(f"Error during authentication: {error_data['error_description']}")
            except TypeError:
                # No json
                await self.print(str(ex))
            sys.exit(1)

        # If we are here, no auth to try
        await self.print('Not enough information was supplied to authenticate')
        return False

    async def save_tokens(self, args):
        if args.tokens_stdout:
            sys.stdout.write(json.dumps(self.tokendata))
        else:
            with codecs.open(self.outfile, 'w', 'utf-8') as outfile:
                json.dump(self.tokendata, outfile)
            await self.print('Tokens were written to {}'.format(self.outfile))

async def amain():
    parser = argparse.ArgumentParser(add_help=True, description='ROADtools Authentication utility', formatter_class=argparse.RawDescriptionHelpFormatter)
    auth = Authentication()
    auth.get_sub_argparse(parser)
    if len(sys.argv) < 2:
        parser.print_help()
        sys.exit(1)
    args = parser.parse_args()
    auth.parse_args(args)
    res = await auth.get_tokens(args)
    print(res)
    if not res:
        return
    await auth.save_tokens(args)

async def consolemainauth(args:str):
    import shlex

    parser = argparse.ArgumentParser(add_help=True, description='ROADtools Authentication utility', formatter_class=argparse.RawDescriptionHelpFormatter)
    auth = Authentication()
    auth.get_sub_argparse(parser)
    args = parser.parse_args(shlex.split(args))
    auth.parse_args(args)
    res = await auth.get_tokens(args)
    if not res:
        return
    auth.save_tokens(args)

    # from aroadtools.roadlib.auth import consolemainauth
    # await consolemainauth("-u testuser@azuretest156k.onmicrosoft.com -p ALMA")

def main():
    asyncio.run(amain())

if __name__ == '__main__':
    main()
