import base64
import binascii
import struct
import json
import sys
import pprint
import random
import string
import codecs
import jwt
import ssl
import os
import time
import warnings
import datetime
import uuid
from cryptography.hazmat.primitives import serialization, padding, hashes
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives.asymmetric import padding as apadding
from cryptography.hazmat.primitives.keywrap import aes_key_unwrap
from cryptography.hazmat.primitives.serialization import pkcs12
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography import x509
from cryptography.x509.oid import NameOID
from cryptography.utils import CryptographyDeprecationWarning
from aroadtools.roadlib.auth import Authentication, get_data, AuthenticationException
import httpx

warnings.filterwarnings("ignore", category=CryptographyDeprecationWarning)


class DeviceAuthentication():
    """
    Device authentication for ROADtools. Handles device registration,
    PRT request/renew and token request using WAM emulation.
    """
    def __init__(self, httptransport=None, httpauth = None):
        self.httptransport = httptransport
        self.httpauth = httpauth
        # Cryptography certificate object
        self.certificate = None
        # Cryptography private key object
        self.privkey = None
        # PEM key data
        self.keydata = None
        # Separate transport key if applicable
        self.transportprivkey = None
        self.transportkeydata = None

        # PRT data
        self.prt = None
        self.session_key = None

        # Windows Hello key - Cryptography private key object
        self.hellokey = None
        # Hello key as PEM
        self.hellokeydata = None

        # Proxies
        self.proxies = {}
        # Verify TLS certs
        self.verify = True

    def loadcert(self, pemfile=None, privkeyfile=None, pfxfile=None, pfxpass=None, pfxbase64=None):
        """
        Load a device certificate from disk
        """
        if pemfile and privkeyfile:
            with open(pemfile, "rb") as certf:
                self.certificate = x509.load_pem_x509_certificate(certf.read())
            with open(privkeyfile, "rb") as keyf:
                self.transportkeydata = self.keydata = keyf.read()
                self.transportprivkey = self.privkey = serialization.load_pem_private_key(self.keydata, password=None)
            return True
        if pfxfile or pfxbase64:
            if pfxfile:
                with open(pfxfile, 'rb') as pfxf:
                    pfxdata = pfxf.read()
            if pfxbase64:
                pfxdata = base64.b64decode(pfxbase64)
            if isinstance(pfxpass, str):
                pfxpass = pfxpass.encode()
            self.privkey, self.certificate, _ = pkcs12.load_key_and_certificates(pfxdata, pfxpass)
            self.transportprivkey = self.privkey
            # PyJWT needs the key as PEM data anyway, so encode it
            self.transportkeydata = self.keydata = self.privkey.private_bytes(
                encoding=serialization.Encoding.PEM,
                format=serialization.PrivateFormat.TraditionalOpenSSL,
                encryption_algorithm=serialization.NoEncryption(),
            )
            return True
        print('You must specify either a PEM certificate file and private key file or a pfx file with the device keypair.')
        return False

    def loadkey(self, privkeyfile=None, pfxfile=None, pfxpass=None, pfxbase64=None, transport_only=False):
        """
        Load private key only (to use as transport key)
        Optionally load it as transport key only and not as device key to support separate transport keys
        """
        if privkeyfile:
            with open(privkeyfile, "rb") as keyf:
                if transport_only:
                    # Only load as transport key
                    self.transportkeydata = keyf.read()
                    self.transportprivkey = serialization.load_pem_private_key(self.keydata, password=None)
                else:
                    self.transportkeydata = self.keydata = keyf.read()
                    self.transportprivkey = self.privkey = serialization.load_pem_private_key(self.keydata, password=None)
            return True
        if pfxfile or pfxbase64:
            if pfxfile:
                with open(pfxfile, 'rb') as pfxf:
                    pfxdata = pfxf.read()
            if pfxbase64:
                pfxdata = base64.b64decode(pfxbase64)
            if isinstance(pfxpass, str):
                pfxpass = pfxpass.encode()
            # Load cert anyway since it's in the same file
            if not transport_only:
                self.privkey, self.certificate, _ = pkcs12.load_key_and_certificates(pfxdata, pfxpass)
                self.transportprivkey = self.privkey
                # PyJWT needs the key as PEM data anyway, so encode it
                self.transportkeydata = self.keydata = self.privkey.private_bytes(
                    encoding=serialization.Encoding.PEM,
                    format=serialization.PrivateFormat.TraditionalOpenSSL,
                    encryption_algorithm=serialization.NoEncryption(),
                )
            else:
                self.transportprivkey, _, _ = pkcs12.load_key_and_certificates(pfxdata, pfxpass)
                self.transportkeydata = self.privkey.private_bytes(
                    encoding=serialization.Encoding.PEM,
                    format=serialization.PrivateFormat.TraditionalOpenSSL,
                    encryption_algorithm=serialization.NoEncryption(),
                )
            return True
        print('You must specify either a private key file or a pfx file with the device keypair.')
        return False

    def loadprt(self, prtfile):
        """
        Load PRT from disk
        """
        if not prtfile:
            return False
        try:
            with codecs.open(prtfile, 'r', 'utf-8') as prtf:
                prtdata = json.load(prtf)
                self.prt = Authentication.ensure_plain_prt(prtdata['refresh_token'])
                self.session_key = Authentication.ensure_binary_sessionkey(prtdata['session_key'])
        except FileNotFoundError:
            return False
        return True

    def setprt(self, prt, sessionkey):
        """
        Set PRT parameters in the correct internal format
        """
        self.prt = Authentication.ensure_plain_prt(prt)
        self.session_key = Authentication.ensure_binary_sessionkey(sessionkey)

    def saveprt(self, prtdata, prtfile):
        """
        Save PRT data to file
        """
        with codecs.open(prtfile, 'w', 'utf-8') as prtf:
            json.dump(prtdata, prtf, sort_keys=True, indent=4)
        print(f"Saved PRT to {prtfile}")

    def loadhellokey(self, privkeyfile):
        """
        Load Windows Hello key from file
        """
        if not privkeyfile:
            return False
        try:
            with open(privkeyfile, "rb") as keyf:
                self.hellokeydata = keyf.read()
                self.hellokey = serialization.load_pem_private_key(self.hellokeydata, password=None)
        except FileNotFoundError:
            return False
        return True

    def get_privkey_kid(self, key=None):
        """
        Get the kid (key ID) for the given key from a file
        """
        if not key:
            key = self.hellokey

        pubkeycngblob = self.create_pubkey_blob_from_key(key)
        digest = hashes.Hash(hashes.SHA256())
        digest.update(pubkeycngblob)
        kid = base64.b64encode(digest.finalize()).decode('utf-8')
        return kid

    def create_hello_key(self, privout=None):
        """
        Create a key for Windows Hello, saving it to a file
        """
        if not privout:
            privout = 'winhello.key'

        # Generate our key
        key = rsa.generate_private_key(
            public_exponent=65537,
            key_size=2048,
        )
        # Write device key to disk
        print(f'Saving private key to {privout}')
        with open(privout, "wb") as keyf:
            keyf.write(key.private_bytes(
                encoding=serialization.Encoding.PEM,
                format=serialization.PrivateFormat.TraditionalOpenSSL,
                encryption_algorithm=serialization.NoEncryption(),
            ))

        pubkeycngblob = base64.b64encode(self.create_pubkey_blob_from_key(key))
        return key, pubkeycngblob

    def create_hello_prt_assertion(self, username):
        now = int(time.time())
        payload = {
          "iss": username,
          # Should be tenant ID, but this is not verified
          "aud": "common",
          "iat": now-3600,
          "exp": now+3600,
          "scope": "openid aza ugs"
        }
        headers = {
            "kid": self.get_privkey_kid(),
            "use": "ngc"
        }
        reqjwt = jwt.encode(payload, algorithm='RS256', key=self.hellokeydata, headers=headers)
        return reqjwt

    async def get_prt_with_hello_key(self, username, assertion=None):
        authlib = Authentication()
        challenge = await authlib.get_srv_challenge()['Nonce']
        if not assertion:
            assertion = self.create_hello_prt_assertion(username)
        # Construct
        payload = {
            "client_id": "38aa3b87-a06d-4817-b275-7a316988d93b",
            "request_nonce": challenge,
            "scope": "openid aza ugs",
            # Not sure if these matter
            "group_sids": [],
            "win_ver": "10.0.19041.868",
            "grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
            # Windows includes this, but it is not required or used
            # user is instead taken from JWT assertion
            "username": username,
            "assertion": assertion
        }
        return await self.request_token_with_devicecert_signed_payload(payload)

    async def register_winhello_key(self, pubkeycngblob, access_token):
        headers = {
            'Authorization': f'Bearer {access_token}',
            'Content-Type': 'application/json',
            'User-Agent': 'Dsreg/10.0 (Windows 10.0.19044.1826)',
            'Accept': 'application/json',
        }
        data = {
            "kngc": pubkeycngblob.decode('utf-8')
        }

        async with httpx.AsyncClient(transport=self.httptransport, auth = self.httpauth) as session:
            res = await session.post('https://enterpriseregistration.windows.net/EnrollmentServer/key/?api-version=1.0', headers=headers, json=data)
            resdata = res.json()
        
        return resdata

    def create_pubkey_blob_from_key(self, key):
        """
        Convert a key (or certificate) to RSA key blob
        https://docs.microsoft.com/en-us/windows/win32/api/bcrypt/ns-bcrypt-bcrypt_rsakey_blob
        """
        pubkey = key.public_key()
        pubnumbers = pubkey.public_numbers()

        # From python docs https://docs.python.org/3/library/stdtypes.html#int.to_bytes
        exponent_as_bytes = pubnumbers.e.to_bytes((pubnumbers.e.bit_length() + 7) // 8, byteorder='big')
        modulus_as_bytes = pubnumbers.n.to_bytes((pubnumbers.n.bit_length() + 7) // 8, byteorder='big')

        header = [
            b'RSA1',
            struct.pack('<L', pubkey.key_size),
            struct.pack('<L', len(exponent_as_bytes)),
            struct.pack('<L', len(modulus_as_bytes)),
            # No private key so these are zero
            struct.pack('<L', 0),
            struct.pack('<L', 0),
        ]

        pubkeycngblob = b''.join(header)+exponent_as_bytes+modulus_as_bytes
        return pubkeycngblob

    def create_public_jwk_from_key(self, key, for_registration=False):
        """
        Convert a key (or certificate) to JWK public numbers
        https://www.rfc-editor.org/rfc/rfc7517
        """
        pubkey = key.public_key()
        pubnumbers = pubkey.public_numbers()

        # From python docs https://docs.python.org/3/library/stdtypes.html#int.to_bytes
        exponent_as_bytes = pubnumbers.e.to_bytes((pubnumbers.e.bit_length() + 7) // 8, byteorder='big')
        modulus_as_bytes = pubnumbers.n.to_bytes((pubnumbers.n.bit_length() + 7) // 8, byteorder='big')


        if for_registration:
            # Registration expects additional parameters and different encoding (regular base64 instead of urlsafe)
            jwk = {
                'kty': 'RSA',
                'e': base64.b64encode(exponent_as_bytes).decode('utf-8'),
                'n': base64.b64encode(modulus_as_bytes).decode('utf-8'),
                'alg': 'RS256',
                'kid': str(uuid.uuid4()).upper()
            }
        else:
            jwk = {
                'kty': 'RSA',
                'e': base64.urlsafe_b64encode(exponent_as_bytes).decode('utf-8'),
                'n': base64.urlsafe_b64encode(modulus_as_bytes).decode('utf-8'),
            }
        return json.dumps(jwk, separators=(',', ':'))

    async def register_device(self, access_token, jointype=0, certout=None, privout=None, device_type=None, device_name=None, os_version=None, deviceticket=None):
        """
        Registers a device in Azure AD. Requires an access token to the device registration service.
        """
        # Fill in names if not supplied
        if not device_name:
            device_name = 'DESKTOP-' + ''.join(random.choices(string.ascii_uppercase + string.digits, k=8))

        if not certout:
            certout = device_name.lower() + '.pem'

        if not privout:
            privout = device_name.lower() + '.key'

        if not device_type:
            device_type = "Windows"

        if not os_version:
            os_version = "10.0.19041.928"

        # Generate our key
        key = rsa.generate_private_key(
            public_exponent=65537,
            key_size=2048,
        )
        # Write device key to disk
        print(f'Saving private key to {privout}')
        with open(privout, "wb") as keyf:
            keyf.write(key.private_bytes(
                encoding=serialization.Encoding.PEM,
                format=serialization.PrivateFormat.TraditionalOpenSSL,
                encryption_algorithm=serialization.NoEncryption(),
            ))

        # Generate a CSR
        csr = x509.CertificateSigningRequestBuilder().subject_name(x509.Name([
            x509.NameAttribute(NameOID.COMMON_NAME, "7E980AD9-B86D-4306-9425-9AC066FB014A"),
        ])).sign(key, hashes.SHA256())

        # Get parameters needed to construct the CNG blob
        certreq = csr.public_bytes(serialization.Encoding.DER)
        certbytes = base64.b64encode(certreq)

        pubkeycngblob = base64.b64encode(self.create_pubkey_blob_from_key(key))


        if device_type.lower() == 'macos':
            data = {
              "DeviceDisplayName" : device_name,
              "CertificateRequest" : {
                "Type" : "pkcs10",
                "Data" : certbytes.decode('utf-8')
              },
              "OSVersion" : "12.2.0",
              "TargetDomain" : "iminyour.cloud",
              "AikCertificate" : "",
              "DeviceType" : "MacOS",
              "TransportKey" : base64.b64encode(self.create_public_jwk_from_key(key, True).encode('utf-8')).decode('utf-8'),
              "JoinType" : jointype,
              "AttestationData" : ""
            }
        else:
            data = {
                "CertificateRequest":
                    {
                        "Type": "pkcs10",
                        "Data": certbytes.decode('utf-8')
                    },
                "TransportKey": pubkeycngblob.decode('utf-8'),
                # Can likely be edited to anything, are not validated afaik
                "TargetDomain": "iminyour.cloud",
                "DeviceType": device_type,
                "OSVersion": os_version,
                "DeviceDisplayName": device_name,
                "JoinType": jointype,
                "attributes": {
                    "ReuseDevice": "true",
                    "ReturnClientSid": "true"
                }
            }
            # Add device ticket if requested
            if deviceticket:
                data['attributes']['MSA-DDID'] = base64.b64encode(deviceticket.encode('utf-8')).decode('utf-8')


        headers = {
            'Authorization': f'Bearer {access_token}',
            'Content-Type': 'application/json'
        }

        print('Registering device')
        async with httpx.AsyncClient(transport=self.httptransport, auth = self.httpauth) as session:
            res = await session.post('https://enterpriseregistration.windows.net/EnrollmentServer/device/?api-version=2.0', headers=headers, json=data)
            returndata = res.json()

            if not 'Certificate' in returndata:
                print('Error registering device! Got response:')
                pprint.pprint(returndata)
                return False
            cert = x509.load_der_x509_certificate(base64.b64decode(returndata['Certificate']['RawBody']))
            # There is only one, so print it
            for attribute in cert.subject:
                print(f"Device ID: {attribute.value}")
            with open(certout, "wb") as certf:
                certf.write(cert.public_bytes(serialization.Encoding.PEM))
            print(f'Saved device certificate to {certout}')
        return True

    async def register_hybrid_device(self, objectsid, tenantid, certout=None, privout=None, device_type=None, device_name=None, os_version=None):
        '''
        Register hybrid device. Requires existing key/cert to be already loaded and the SID to be specified.
        Device should be synced to AAD already, otherwise this will fail.
        '''
        # Fill in names if not supplied
        if not device_name:
            device_name = 'DESKTOP-' + ''.join(random.choices(string.ascii_uppercase + string.digits, k=8))

        certout = device_name.lower() + '_aad.pem'
        privout = device_name.lower() + '_aad.key'

        if not device_type:
            device_type = "Windows"

        if not os_version:
            os_version = "10.0.19041.928"

        # Generate our new shiny key
        key = rsa.generate_private_key(
            public_exponent=65537,
            key_size=2048,
        )
        # Write device key to disk
        print(f'Saving private key to {privout}')
        with open(privout, "wb") as keyf:
            keyf.write(key.private_bytes(
                encoding=serialization.Encoding.PEM,
                format=serialization.PrivateFormat.TraditionalOpenSSL,
                encryption_algorithm=serialization.NoEncryption(),
            ))

        # Generate a CSR
        csr = x509.CertificateSigningRequestBuilder().subject_name(x509.Name([
            x509.NameAttribute(NameOID.COMMON_NAME, "7E980AD9-B86D-4306-9425-9AC066FB014A"),
        ])).sign(key, hashes.SHA256())

        # Get parameters needed to construct the CNG blob
        certreq = csr.public_bytes(serialization.Encoding.DER)
        certbytes = base64.b64encode(certreq)

        pubkeycngblob = base64.b64encode(self.create_pubkey_blob_from_key(key))
        curtime = datetime.datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')
        signdata = f"{objectsid}.{curtime}Z"
        signedblob = base64.b64encode(self.privkey.sign(
            signdata.encode('utf-8'),
            apadding.PKCS1v15(),
            hashes.SHA256()
        ))

        data = {
            "CertificateRequest":
                {
                    "Type": "pkcs10",
                    "Data": certbytes.decode('utf-8')
                },
            "ServerAdJoinData":
            {
                "TransportKey": pubkeycngblob.decode('utf-8'),
                "TargetDomain": "iminyour.cloud",
                "DeviceType": device_type,
                "OSVersion": os_version,
                "DeviceDisplayName": device_name,
                "TargetDomainId": f"{tenantid}",
                "ClientIdentity":
                {
                    "Type": "sha256signed",
                    "Sid": signdata,
                    "SignedBlob": signedblob.decode('utf-8')
                }
            },
            # Hybrid join = 6
            "JoinType": 6,
            "attributes": {
                "ReuseDevice": "true",
                "ReturnClientSid": "true"
            }
        }

        headers = {
            'User-Agent': f'Dsreg/10.0 (Windows {os_version})',
            'Content-Type': 'application/json'
        }
        # Extract device ID from certificate
        for attr in self.certificate.subject:
            deviceid = attr.value
        print(f"Device ID (from certificate): {deviceid}")
        print('Registering device')

        async with httpx.AsyncClient(transport=self.httptransport, auth = self.httpauth) as session:
            res = await session.post(f'https://enterpriseregistration.windows.net/EnrollmentServer/device/{deviceid}?api-version=2.0', headers=headers, json=data)
            returndata = res.json()

            if not 'Certificate' in returndata:
                print('Error registering device! Got response:')
                pprint.pprint(returndata)
                return False
            cert = x509.load_der_x509_certificate(base64.b64decode(returndata['Certificate']['RawBody']))
            # There is only one, so print it
            for attribute in cert.subject:
                print(f"AAD device ID: {attribute.value}")
            with open(certout, "wb") as certf:
                certf.write(cert.public_bytes(serialization.Encoding.PEM))
            print(f'Saved device certificate to {certout}')
        return True

    async def delete_device(self, certpath, keypath):
        """
        Delete the device from Azure ID. Requires client cert auth
        """
        # Get device ID from certificate
        deviceid = None
        for attribute in self.certificate.subject:
            deviceid = attribute.value
        if not deviceid:
            return
        
        ssl_ctx = ssl.create_default_context()
        ssl_ctx.load_cert_chain(certpath, keypath)
        
        newtransport = None
        verify = ssl_ctx
        if self.httptransport is not None:
            newtransport = self.httptransport.create_new(ssl_context=ssl_ctx)
            verify = None
        
        
        async with httpx.AsyncClient(transport=newtransport, auth = self.httpauth, verify = verify, follow_redirects=True) as session:
            res = await session.delete(f'https://enterpriseregistration.windows.net/EnrollmentServer/device/{deviceid}?')
            resdata = res.text()

            if res.status_code != 200:
                print('Error deleting device:')
                print(resdata)
                return False
            print('Device was deleted in Azure AD')
        return True

    async def request_token_with_devicecert_signed_payload(self, payload):
        """
        Wrap the request payload in a JWT and sign this using the device cert / key
        """
        certder = self.certificate.public_bytes(serialization.Encoding.DER)
        certbytes = base64.b64encode(certder)
        headers = {
          "x5c": certbytes.decode('utf-8'),
          "kdf_ver": 2
        }
        reqjwt = jwt.encode(payload, algorithm='RS256', key=self.keydata, headers=headers)
        prt_request_data = {
            'windows_api_version':'2.2',
            'grant_type':'urn:ietf:params:oauth:grant-type:jwt-bearer',
            'request':reqjwt,
            'client_info':'1',
            'tgt':True
        }

        prtdata = None
        async with httpx.AsyncClient(transport=self.httptransport, auth = self.httpauth) as session:
            res = await session.post('https://login.microsoftonline.com/common/oauth2/token', data=prt_request_data)
            if res.status_code != 200:
                errmsg = res.text
                raise AuthenticationException(errmsg)
                
            prtdata = res.json()
            # Encrypted session key that we need to unwrap
            sessionkey_jwe = prtdata['session_key_jwe']
            uwk = self.decrypt_jwe_with_transport_key(sessionkey_jwe)

            prtdata['session_key'] = binascii.hexlify(uwk).decode('utf-8')
            # Decrypt Kerberos keys
            authlib = Authentication()
            for tgt in ['tgt_ad', 'tgt_cloud']:
                if tgt in prtdata:
                    tgtdata = json.loads(prtdata[tgt])
                    if tgtdata['keyType'] != 0:
                        # There is a key
                        tgt_sessionkey = authlib.decrypt_auth_response(tgtdata['clientKey'], uwk)
                        prtdata[tgt + '_sessionkey'] = binascii.hexlify(tgt_sessionkey).decode('utf-8')

        return prtdata

    def decrypt_jwe_with_transport_key(self, jwetoken):
        """
        Decrypt JWE data structure with transport key to obtain session key
        """
        dataparts = jwetoken.split('.')

        # Decode
        wrapped_key = get_data(dataparts[1])

        # borrowed from https://github.com/mpdavis/python-jose/blob/16bde1737d8a4f498db2333e5fc7d191e0fc915f/jose/backends/cryptography_backend.py#L514
        uwk = self.transportprivkey.decrypt(wrapped_key, apadding.OAEP(apadding.MGF1(hashes.SHA1()), hashes.SHA1(), None))
        return uwk

    async def request_token_with_sessionkey_signed_payload(self, payload, reqtgt=True):
        """
        Request a token (access / refresh / PRT) using a payload signed
        with the PRT session key.
        """
        authlib = Authentication()
        context = os.urandom(24)
        headers = {
            'ctx': base64.b64encode(context).decode('utf-8'), #.rstrip('=')
            'kdf_ver': 2
        }
        # Sign with random key just to get jwt body in right encoding
        tempjwt = jwt.encode(payload, os.urandom(32), algorithm='HS256', headers=headers)
        jbody = tempjwt.split('.')[1]
        jwtbody = base64.b64decode(jbody+('='*(len(jbody)%4)))

        # Now calculate the derived key based on random context plus jwt body
        _, derived_key = authlib.calculate_derived_key_v2(self.session_key, context, jwtbody)
        reqjwt = jwt.encode(payload, derived_key, algorithm='HS256', headers=headers)

        token_request_data = {
            'windows_api_version':'2.2',
            'grant_type':'urn:ietf:params:oauth:grant-type:jwt-bearer',
            'request':reqjwt,
            'client_info':'1'
        }
        if reqtgt:
            token_request_data['tgt'] = True
        
        async with httpx.AsyncClient(transport=self.httptransport, auth = self.httpauth) as session:
            res = await session.post('https://login.microsoftonline.com/common/oauth2/token', data=token_request_data)
            responsedata = res.text()
            if res.status_code != 200:
                raise AuthenticationException(responsedata)
               
            return responsedata
        return None

    async def get_prt_with_password(self, username, password):
        authlib = Authentication()
        challenge = await authlib.get_srv_challenge()['Nonce']
        # Construct
        payload = {
            "client_id": "38aa3b87-a06d-4817-b275-7a316988d93b",
            "request_nonce": challenge,
            "scope": "openid aza ugs",
            # Not sure if these matter
            "group_sids": [],
            "win_ver": "10.0.19041.868",
            "grant_type": "password",
            "username": username,
            "password": password,
        }
        return await self.request_token_with_devicecert_signed_payload(payload)

    async def get_prt_with_samltoken(self, samltoken):
        authlib = Authentication()
        challenge = await authlib.get_srv_challenge()['Nonce']
        # Construct
        payload = {
            "client_id": "38aa3b87-a06d-4817-b275-7a316988d93b",
            "request_nonce": challenge,
            "scope": "openid aza ugs",
            # Not sure if these matter
            "group_sids": [],
            "win_ver": "10.0.19041.868",
            "grant_type": "urn:ietf:params:oauth:grant-type:saml1_1-bearer",
            "assertion": base64.b64encode(samltoken.encode('utf-8')).decode('utf-8'),
        }
        return await self.request_token_with_devicecert_signed_payload(payload)

    async def get_prt_with_refresh_token(self, refresh_token):
        authlib = Authentication()
        challenge = (await authlib.get_srv_challenge())['Nonce']

        payload = {
            "client_id": "29d9ed98-a469-4536-ade2-f981bc1d605e",
            "request_nonce": challenge,
            "scope": "openid aza ugs",
            # Not sure if these matter
            "group_sids": [],
            "win_ver": "10.0.19041.868",
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
        }
        return await self.request_token_with_devicecert_signed_payload(payload)

    async def renew_prt(self):
        authlib = Authentication()
        challenge = await authlib.get_srv_challenge()['Nonce']

        payload = {
            "client_id": "38aa3b87-a06d-4817-b275-7a316988d93b",
            "request_nonce": challenge,
            "scope": "openid aza ugs",
            "iss": "aad:brokerplugin",
            "grant_type": "refresh_token",
            "refresh_token": self.prt,
            "previous_refresh_token": self.prt,
            # Not sure if these matter
            "group_sids": [],
            "win_ver": "10.0.19041.868",
        }
        responsedata = await self.request_token_with_sessionkey_signed_payload(payload)
        prtdata = authlib.decrypt_auth_response(responsedata, self.session_key, True)
        prtdata['session_key'] = binascii.hexlify(self.session_key).decode('utf-8')
        return prtdata

    async def aad_brokerplugin_prt_auth(self, client_id, resource, renew_prt=False, redirect_uri=None):
        """
        Auth using a PRT emulating the AAD Brokerplugin (WAM) client
        """
        authlib = Authentication()
        challenge = await authlib.get_srv_challenge()['Nonce']
        client = authlib.lookup_client_id(client_id).lower()
        payload = {
          "win_ver": "10.0.19041.1620",
          "scope": "openid",
          "resource": authlib.lookup_resource_uri(resource),
          "request_nonce": challenge,
          "refresh_token": self.prt,
          "redirect_uri": f"ms-appx-web://Microsoft.AAD.BrokerPlugin/{client}",
          "iss": "aad:brokerplugin",
          "grant_type": "refresh_token",
          "client_id": f"{client}",
          "aud": "login.microsoftonline.com"
        }
        # Request a new PRT, otherwise normal refresh token will be issued
        if renew_prt:
            payload['scope'] = "openid aza"
        # Custom redirect_uri if needed
        if redirect_uri:
            payload['redirect_uri'] = redirect_uri
        elif client == '1b730954-1685-4b74-9bfd-dac224a7b894':
            payload['redirect_uri'] = 'https://login.microsoftonline.com/common/oauth2/nativeclient'
        responsedata = await self.request_token_with_sessionkey_signed_payload(payload, False)
        tokendata = authlib.decrypt_auth_response(responsedata, self.session_key, True)
        return tokendata
