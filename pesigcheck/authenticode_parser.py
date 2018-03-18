#!/usr/bin/env python

# This is a derivative, modified, work from the verify-sigs project.
# Please refer to the LICENSE file in the distribution for more
# information. Original filename: auth_data.py
#
# Parts of this file are licensed as follows:
#
# Copyright 2010 Google Inc. All Rights Reserved.
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

"""This module effectively implements the first few chapters of Microsoft's documentation on Authenticode_PE_.

.. _Authenticode_PE: http://download.microsoft.com/download/9/c/5/9c5b2167-8017-4bae-9fde-d599bac8184a/Authenticode_PE.docx
"""

import hashlib
import logging
import pathlib

import datetime

from cryptography import x509
from cryptography.exceptions import InvalidSignature, UnsupportedAlgorithm
from cryptography.hazmat.backends import default_backend
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import rsa, padding, ec
from pyasn1.codec.ber import decoder as ber_decoder
from pyasn1.codec.der import encoder as der_encoder
from pyasn1.codec.der import decoder as der_decoder
from pyasn1.type import univ

from . import asn1

logger = logging.getLogger(__name__)

ACCEPTED_DIGEST_ALGORITHMS = (hashlib.md5, hashlib.sha1)
CERTIFICATE_LOCATION = pathlib.Path(__file__).resolve().parent.parent / "certificates" / "authenticode"


class AuthenticodeParseError(Exception):
    pass


class AuthenticodeVerificationError(Exception):
    pass


def _print_type(t):
    if t is None:
        return ""
    elif isinstance(t, tuple):
        return ".".join(t)
    elif hasattr(t, "__name__"):
        return t.__name__
    else:
        return type(t).__name__


def _guarded_ber_decode(data, asn1_spec=None):
    result, rest = ber_decoder.decode(data, asn1Spec=asn1_spec)
    if rest:
        raise AuthenticodeParseError("Extra information after parsing %s BER" % _print_type(asn1_spec))
    return result


def _verify_empty_algorithm_parameters(algorithm, location):
    if 'parameters' in algorithm and algorithm['parameters'].isValue:
        parameters = _guarded_ber_decode(algorithm['parameters'])
        if not isinstance(parameters, univ.Null):
            raise AuthenticodeParseError("%s has parameters set, which is unexpected" % (location,))


def _get_digest_algorithm(algorithm, location):
    result = asn1.oids.get(algorithm['algorithm'])
    if result not in ACCEPTED_DIGEST_ALGORITHMS:
        raise AuthenticodeParseError("%s must be one of %s, not %s" %
                                     (location, [x().name for x in ACCEPTED_DIGEST_ALGORITHMS], result().name))

    _verify_empty_algorithm_parameters(algorithm, location)
    return result


def _get_encryption_algorithm(algorithm, location):
    result = asn1.oids.OID_TO_PUBKEY.get(algorithm['algorithm'])
    if result is None:
        raise AuthenticodeParseError("%s: %s is not acceptable as encryption algorithm" %
                                     (location, algorithm['algorithm']))

    _verify_empty_algorithm_parameters(algorithm, location)
    return result


class CertificateStore(list):
    def __init__(self, *args, trusted=False, **kwargs):
        super().__init__(*args, **kwargs)
        self.trusted = trusted

    def append(self, elem):
        elem.trusted = self.trusted
        return super().append(elem)


class FileSystemCertificateStore(CertificateStore):
    _loaded = False

    def __init__(self, location, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.location = location

    def __iter__(self):
        self._load()  # TODO: load whenever needed.
        return super().__iter__()

    def _load(self):
        if self._loaded:
            return
        self._loaded = True

        for file in self.location.glob("*"):
            with open(str(file), "rb") as f:
                x590_cert = x509.load_pem_x509_certificate(f.read(), default_backend())
            cert = Certificate(der_decoder.decode(x590_cert.tbs_certificate_bytes,
                                                  asn1Spec=asn1.x509.TBSCertificate())[0])
            cert._x509 = x590_cert
            self.append(cert)


trusted_certificate_store = FileSystemCertificateStore(location=CERTIFICATE_LOCATION, trusted=True)


class CertificateVerificationContext(object):
    def __init__(self, *stores, timestamp=None, extended_key_usage=None, allow_legacy=True):
        """A Context holding

        :param Iterable[CertificateStore] stores: A list of CertificateStore objects that contain certificates
        :param datetime.datetime timestamp: The timestamp to verify with. If None, the current time is used.
            Must be a timezone-aware timestamp.
        :param tuple extended_key_usage: A tuple with the OID of an EKU to check for. Typical values are
            asn1.oids.EKU_CODE_SIGNING and asn1.oids.EKU_TIME_STAMPING
        :param bool allow_legacy: If True, allows chain verification using pyOpenSSL if the signature hash algorithm
            is too old to be supported by cryptography (e.g. MD2).
        """

        self.stores = stores

        if timestamp is None:
            timestamp = datetime.datetime.now(datetime.timezone.utc)
        self.timestamp = timestamp
        self.extended_key_usage = extended_key_usage
        self.allow_legacy = allow_legacy

    @property
    def certificates(self):
        """Iterates over all certificates in the associated stores.

        :rtype: Iterable[Certificate]
        """
        for store in self.stores:
            yield from store

    def find_certificates(self, *, subject=None, serial_number=None, issuer=None):
        """Finds all certificates given by the specified properties. A property can be omitted by specifying
        :const:`None`. Calling this function without arguments is the same as using :meth:`certificates`

        :param pesigcheck.asn1.x509.Name subject: Certificate subject to look for.
        :param int serial_number: Serial number to look for.
        :param pesigcheck.asn1.x509.Name issuer: Certificate issuer to look for.
        :rtype: Iterable[Certificate]
        """

        for certificate in self.certificates:
            if subject is not None and certificate.subject != subject:
                continue
            if serial_number is not None and certificate.serial_number != serial_number:
                continue
            if issuer is not None and certificate.issuer != issuer:
                continue
            yield certificate


class Certificate(object):
    def __init__(self, data):
        self.data = data
        self.trusted = False
        self._x509 = None
        self._parse()

    def _parse(self):
        if isinstance(self.data, asn1.pkcs7.ExtendedCertificateOrCertificate):
            if 'extendedCertificate' in self.data:
                # TODO: Not sure if needed.
                raise NotImplementedError("Support for extendedCertificate is not implemented")

            certificate = self.data['certificate']
            self.signature_algorithm = certificate['signatureAlgorithm']
            self.signature_value = certificate['signatureValue']
            tbs_certificate = certificate['tbsCertificate']

        elif isinstance(self.data, asn1.x509.Certificate):
            certificate = self.data
            self.signature_algorithm = certificate['signatureAlgorithm']
            self.signature_value = certificate['signatureValue']
            tbs_certificate = certificate['tbsCertificate']

        else:
            tbs_certificate = self.data

        self.version = int(tbs_certificate['version']) + 1
        self.serial_number = int(tbs_certificate['serialNumber'])
        self.issuer = tbs_certificate['issuer'][0]
        self.issuer_dn = tbs_certificate['issuer'][0].to_string()
        self.valid_from = tbs_certificate['validity']['notBefore'].to_python_time()
        self.valid_to = tbs_certificate['validity']['notAfter'].to_python_time()
        self.subject = tbs_certificate['subject'][0]
        self.subject_dn = tbs_certificate['subject'][0].to_string()

        self.subject_public_algorithm = tbs_certificate['subjectPublicKeyInfo']['algorithm']
        self.subject_public_key = tbs_certificate['subjectPublicKeyInfo']['subjectPublicKey']

        self.extensions = {}
        if 'extensions' in tbs_certificate and tbs_certificate['extensions'].isValue:
            for extension in tbs_certificate['extensions']:
                self.extensions[asn1.oids.get(extension['extnID'])] = extension['extnValue']

    def __str__(self):
        if self.trusted:
            return "{}(serial:{})(trusted)".format(self.subject_dn, self.serial_number)
        return "{}(serial:{})".format(self.subject_dn, self.serial_number)

    @property
    def x509(self):
        if self._x509 is None:
            self._x509 = x509.load_der_x509_certificate(der_encoder.encode(self.data), default_backend())
        return self._x509

    def _verify_certificate(self, context):
        """Verifies some basic properties of the certificate, including:

        * Its validity period
        * Its ExtendedKeyUsage if required by the context.
        """
        if not self.valid_from <= context.timestamp <= self.valid_to:
            raise AuthenticodeVerificationError("Certificate {cert} is outside its validity period. It is valid from "
                                                "{valid_from} to {valid_to}, but we checked it against {timestamp}"
                                                .format(cert=self, timestamp=context.timestamp,
                                                        valid_from=self.valid_from, valid_to=self.valid_to))

        # Verify extendedKeyUsage
        try:
            extended_key_usage = self.x509.extensions.get_extension_for_class(x509.ExtendedKeyUsage).value
        except x509.ExtensionNotFound:
            pass
        else:
            extended_key_usage_ = list(map(lambda x: tuple(map(int, x.dotted_string.split("."))), extended_key_usage))

            if context.extended_key_usage is not None and \
                    x509.oid.ExtendedKeyUsageOID.ANY_EXTENDED_KEY_USAGE not in extended_key_usage and \
                    context.extended_key_usage not in extended_key_usage_:
                raise AuthenticodeVerificationError("Certificate %s does not have %s in its extendedKeyUsage" %
                                                    (self, context.extended_key_usage))

    def verify_signature(self, signature, data, algorithm):
        """Verifies whether the signature bytes match the data using the hashing algorithm. Supports RSA and EC keys.
        Note that not all hashing algorithms are supported by the cryptography module.

        :param bytes signature: The signature to verify
        :param bytes data: The data that must be verified
        :type algorithm: cryptography.hazmat.primitives.hashes.HashAlgorithm or hashlib.function
        :param algorithm: The hashing algorithm to use
        """

        # Given a hashlib.sha1 object, convert it to the appropritate value
        algorithm = {
            hashlib.md5: hashes.MD5(),
            hashlib.sha1: hashes.SHA1(),
            hashlib.sha224: hashes.SHA224(),
            hashlib.sha256: hashes.SHA256(),
            hashlib.sha384: hashes.SHA384(),
            hashlib.sha512: hashes.SHA512(),
        }.get(algorithm, algorithm)

        public_key = self.x509.public_key()
        if isinstance(public_key, rsa.RSAPublicKey):
            try:
                public_key.verify(signature, data, padding.PKCS1v15(), algorithm)
            except InvalidSignature as e:
                raise AuthenticodeVerificationError("Invalid RSA signature for %s" % self)

        elif isinstance(public_key, ec.EllipticCurvePublicKey):
            try:
                public_key.verify(signature, data, ec.ECDSA(algorithm))
            except InvalidSignature:
                raise AuthenticodeVerificationError("Invalid EC signature for %s" % self)
        else:
            raise AuthenticodeVerificationError("Unknown public key type for certificate %s" % self)

    def _legacy_verify_issuer(self, issuer, timestamp=None):
        """Given an issuer, falls back to using pyOpenSSL to verify the signature.

        :param Certificate issuer: The issuer to verify against
        :raises AuthenticodeVerificationError: if the verification was performed and unsuccessful
        :return: True if the verification was performed and succeeded, False if it was not performed
        """
        try:
            from OpenSSL import crypto
        except ImportError:
            logger.debug("pyopenssl is not installed, legacy verification not available.")
            return False

        store = crypto.X509Store()
        store.add_cert(crypto.X509.from_cryptography(issuer.x509))
        if timestamp is not None:
            store.set_time(timestamp)
        context = crypto.X509StoreContext(store, crypto.X509.from_cryptography(self.x509))
        try:
            context.verify_certificate()
        except crypto.X509StoreContextError as e:
            raise AuthenticodeVerificationError("Failed verifying the certificate using the legacy method: %s" % e)

        return True

    def _verify_issuer(self, issuer, context, depth=0):
        """Verifies whether the provided issuer is a valid issuer for the current certificate.

        The following checks are performed:

        * If BasicConstraints is present, whether the CA attribute is present
        * If KeyUsage is present, whether the Certificate Signing key usage is allowed
        * Verifies the issuing certificate using :meth:`_verify_certificate`, e.g. on its ExtendedKeyUsage and validity
        * Verifies the public key against the signature in the certificate

        The latter is normally performed using cryptography, but if an old hashing algorithm is used, pyOpenSSL is used
        instead, if this is available and allowed by the context.

        :param Certificate issuer: the issuer to verify
        :param CertificateVerificationContext context: Used for some parameters, such as the timestamp.
        :param int depth: The current verification depth, used to check the BasicConstraints
        :raises AuthenticodeVerificationError: when this is not a valid issuer
        """

        issuer._verify_certificate(context)

        # Verify BasicConstraints
        try:
            basic_constraints = issuer.x509.extensions.get_extension_for_class(x509.BasicConstraints).value
        except x509.ExtensionNotFound:
            if issuer.version <= 2:
                logger.warning("Certificate %s does not have the BasicConstraints extension" % issuer)
            else:
                raise AuthenticodeVerificationError("Certificate %s does not have the BasicConstraints extension" %
                                                    issuer)
        else:
            if not basic_constraints.ca:
                raise AuthenticodeVerificationError("Certificate %s does not have CA in its BasicConstraints" % issuer)
            if basic_constraints.path_length is not None and basic_constraints.path_length < depth:
                raise AuthenticodeVerificationError("Certificate %s is at depth %d, whereas its maximum is %s" %
                                                    (self, depth, basic_constraints.path_length))

        # Verify KeyUsage
        try:
            key_usage = issuer.x509.extensions.get_extension_for_class(x509.KeyUsage).value
        except x509.ExtensionNotFound:
            if issuer.version <= 2 or issuer.trusted:
                logger.warning("Certificate %s does not have the KeyUsage extension" % issuer)
            else:
                raise AuthenticodeVerificationError("Certificate %s does not have the KeyUsage extension" % issuer)
        else:
            if not key_usage.key_cert_sign:
                raise AuthenticodeVerificationError("Certificate %s does not have keyCertSign set in its KeyUsage" %
                                                    issuer)

        # Verify the signature
        try:
            issuer.verify_signature(self.x509.signature, self.x509.tbs_certificate_bytes,
                                    self.x509.signature_hash_algorithm)
        except UnsupportedAlgorithm:
            logger.info("The hashing algorithm is not supported by the cryptography module. "
                        "Trying pyopenssl instead")
            if not context.allow_legacy or not self._legacy_verify_issuer(issuer, context.timestamp):
                raise AuthenticodeVerificationError("The algorithm is too old to use by cryptography and pyOpenSSL "
                                                    "is not installed, or legacy checking is disallowed.")

        return True

    def _build_chain(self, context, depth=0):
        """Given a context, builds a chain up to a trusted certificate.

        :param CertificateVerificationContext context: The context for building the chain. Most importantly, contains
            all certificates to build the chain from, but also ther properties are relevant.
        :param depth: The depth of the chain building. Used for recursive calling of this method.
        :return: Iterable of all of the valid chains from this certificate up to and including a trusted anchor.
        :rtype: Iterable[Iterable[Certificate]]
        :raises AuthenticodeVerificationError: if no valid chain was found, which may be an underlying error.
        """
        if depth > 10:
            return
        if self.trusted:
            yield [self]
            return
        # TODO: check when we must stop.
        if self.issuer == self.subject:
            return

        succeeded, last_error = False, None
        for issuer in context.find_certificates(subject=self.issuer):
            try:
                self._verify_issuer(issuer, context, depth)
            except AuthenticodeVerificationError as e:
                last_error = e
            else:
                succeeded = True
                # TODO: issuer._build_chain may also raise errors
                for chain in issuer._build_chain(context, depth+1):
                    yield [self] + chain

        if not succeeded:
            if last_error:
                raise last_error
            else:
                raise AuthenticodeVerificationError("No valid chain found from certificate {}".format(self))

    def verify(self, context):
        """Verifies the certificate, and its chain.

        :param CertificateVerificationContext context: The context for verifying the certificate.
        :return: A list of valid certificate chains for this certificate.
        :rtype: Iterable[Iterable[Certificate]]
        :raises AuthenticodeVerificationError: When the certificate could not be verified.
        """

        self._verify_certificate(context)
        chains = list(self._build_chain(context))

        return chains


class SignerInfo(object):
    _expected_content_type = asn1.spc.SpcIndirectDataContent
    _required_authenticated_attributes = (asn1.pkcs7.ContentType, asn1.pkcs7.Digest, asn1.spc.SpcSpOpusInfo)

    def __init__(self, data):
        self.data = data
        self._parse()

    def _parse(self):
        if self.data['version'] != 1:
            raise AuthenticodeParseError("SignerInfo.version must be 1, not %d" % self.data['version'])

        self.issuer = self.data['issuerAndSerialNumber']['issuer']
        self.issuer_dn = self.data['issuerAndSerialNumber']['issuer'][0].to_string()
        self.serial_number = self.data['issuerAndSerialNumber']['serialNumber']

        self.digest_algorithm = _get_digest_algorithm(self.data['digestAlgorithm'],
                                                      location="SignerInfo.digestAlgorithm")

        self.authenticated_attributes = self._parse_attributes(
            self.data['authenticatedAttributes'],
            required=self._required_authenticated_attributes
        )
        self._encoded_authenticated_attributes = self._encode_attributes(self.data['authenticatedAttributes'])

        # Parse the content of the authenticated attributes
        # - Retrieve object from SpcSpOpusInfo from the authenticated attributes (for normal signer)
        self.program_name = self.more_info = None
        if asn1.spc.SpcSpOpusInfo in self.authenticated_attributes:
            if len(self.authenticated_attributes[asn1.spc.SpcSpOpusInfo]) != 1:
                raise AuthenticodeParseError("Only one SpcSpOpusInfo expected in SignerInfo.authenticatedAttributes")

            self.program_name = self.authenticated_attributes[asn1.spc.SpcSpOpusInfo][0]['programName'].to_python()
            self.more_info = str(self.authenticated_attributes[asn1.spc.SpcSpOpusInfo][0]['moreInfo']['url'])

        # - The messageDigest
        self.message_digest = None
        if asn1.pkcs7.Digest in self.authenticated_attributes:
            if len(self.authenticated_attributes[asn1.pkcs7.Digest]) != 1:
                raise AuthenticodeParseError("Only one Digest expected in SignerInfo.authenticatedAttributes")

            self.message_digest = bytes(self.authenticated_attributes[asn1.pkcs7.Digest][0])

        # - The contentType
        self.content_type = None
        if asn1.pkcs7.ContentType in self.authenticated_attributes:
            if len(self.authenticated_attributes[asn1.pkcs7.ContentType]) != 1:
                raise AuthenticodeParseError("Only one ContentType expected in SignerInfo.authenticatedAttributes")

            self.content_type = asn1.oids.get(self.authenticated_attributes[asn1.pkcs7.ContentType][0])

            if self.content_type is not self._expected_content_type:
                raise AuthenticodeParseError("Unexpected content type for SignerInfo, expected %s, got %s" %
                                             (_print_type(self.content_type),
                                            _print_type(self._expected_content_type)))

        # - The signingTime (used by countersigner)
        self.signing_time = None
        if asn1.pkcs7.SigningTime in self.authenticated_attributes:
            if len(self.authenticated_attributes[asn1.pkcs7.SigningTime]) != 1:
                raise AuthenticodeParseError("Only one SigningTime expected in SignerInfo.authenticatedAttributes")

            self.signing_time = self.authenticated_attributes[asn1.pkcs7.SigningTime][0].to_python_time()

        # Continue with the other attributes of the SignerInfo object
        self.digest_encryption_algorithm = _get_encryption_algorithm(self.data['digestEncryptionAlgorithm'],
                                                                     location="SignerInfo.digestEncryptionAlgorithm")

        self.encrypted_digest = bytes(self.data['encryptedDigest'])

        self.unauthenticated_attributes = self._parse_attributes(self.data['unauthenticatedAttributes'])

        # - The countersigner
        self.countersigner = None
        if asn1.pkcs7.CountersignInfo in self.unauthenticated_attributes:
            if len(self.unauthenticated_attributes[asn1.pkcs7.CountersignInfo]) != 1:
                raise AuthenticodeParseError("Only one CountersignInfo expected in SignerInfo.unauthenticatedAttributes")

            self.countersigner = CounterSignerInfo(self.unauthenticated_attributes[asn1.pkcs7.CountersignInfo][0])

    @classmethod
    def _parse_attributes(cls, data, required=()):
        """Given a set of Attributes, parses them and returns them as a dict

        :param data: The authenticatedAttributes or unauthenticatedAttributes to process
        :param required: A list of required attributes
        """
        result = {}
        for attr in data:
            typ = asn1.oids.get(attr['type'])
            values = []
            for value in attr['values']:
                value = _guarded_ber_decode(value, asn1_spec=typ() if not isinstance(typ, tuple) else None)
                values.append(value)
            result[typ] = values

        if not all((x in result for x in required)):
            raise AuthenticodeParseError("Not all required attributes found. Required: %s; Found: %s" %
                                         ([_print_type(x) for x in required], [_print_type(x) for x in result]))

        return result

    @classmethod
    def _encode_attributes(cls, data):
        """Given a set of Attributes, sorts them in the correct order. They need to be sorted in ascending order in the
        SET, when DER encoded. This also makes sure that the tag on Attributes is correct.

        :param data: The authenticatedAttributes or unauthenticatedAttributes to encode
        """
        sorted_data = sorted([der_encoder.encode(i) for i in data])
        new_attrs = asn1.pkcs7.Attributes()
        for i, attribute in enumerate(sorted_data):
            d, _ = ber_decoder.decode(attribute, asn1Spec=asn1.pkcs7.Attribute())
            new_attrs.setComponentByPosition(i, d)
        return der_encoder.encode(new_attrs)

    def _verify_issuer(self, issuer, context):
        """Verifies whether the given issuer is valid for the given context. Similar to
        :meth:`Certificate._verify_issuer`. Does not support legacy verification method.

        :param Certificate issuer: The Certificate to verify
        :param CertificateVerificationContext context: The
        """

        issuer.verify(context)
        try:
            issuer.verify_signature(self.encrypted_digest,
                                    self._encoded_authenticated_attributes,
                                    self.digest_algorithm)
        except AuthenticodeVerificationError as e:
            raise AuthenticodeVerificationError("Could not verify {cert} as the signer of the authenticated attributes "
                                                "in {cls}: {exc}"
                                                .format(cert=issuer, cls=type(self).__name__, exc=e))

    def _build_chain(self, context):
        succeeded, last_error = False, None
        for issuer in context.find_certificates(issuer=self.issuer, serial_number=self.serial_number):
            try:
                self._verify_issuer(issuer, context)
            except AuthenticodeVerificationError as e:
                last_error = e
            else:
                succeeded = True
                # TODO: _build_chain may also raise errors
                yield from issuer._build_chain(context)

        if not succeeded:
            if last_error:
                raise last_error
            else:
                raise AuthenticodeVerificationError("No valid chain found from {}".format(type(self).__name__))

    def verify(self, context):
        """Verifies the SignerInfo, and its chain.

        :param CertificateVerificationContext context: The context for verifying the SignerInfo.
        :return: A list of valid certificate chains for this SignerInfo.
        :rtype: Iterable[Iterable[Certificate]]
        :raises AuthenticodeVerificationError: When the SignerInfo could not be verified.
        """

        return list(self._build_chain(context))


class CounterSignerInfo(SignerInfo):
    _required_authenticated_attributes = (asn1.pkcs7.ContentType, asn1.pkcs7.SigningTime, asn1.pkcs7.Digest)
    _expected_content_type = asn1.pkcs7.Data


class SpcInfo(object):
    def __init__(self, data, signed_data):
        self.data = data
        self.signed_data = signed_data
        self._parse()

    def _parse(self):
        # The data attribute
        self.content_type = asn1.oids.OID_TO_CLASS.get(self.data['data']['type'])
        self.image_data = None
        if 'value' in self.data['data'] and self.data['data']['value'].isValue:
            self.image_data = None
            # TODO: not parsed
            #image_data = _guarded_ber_decode((self.data['data']['value'], asn1_spec=self.content_type())

        self.digest_algorithm = _get_digest_algorithm(self.data['messageDigest']['digestAlgorithm'],
                                                      location="SpcIndirectDataContent.digestAlgorithm")

        self.digest = bytes(self.data['messageDigest']['digest'])


class SignedData(object):
    def __init__(self, data, pefile=None):
        self.data = data
        self.pefile = pefile
        self._parse()

    @classmethod
    def from_certificate(cls, data, *args, **kwargs):
        # This one is not guarded, which is intentional
        content, rest = ber_decoder.decode(data, asn1Spec=asn1.pkcs7.ContentInfo())
        if asn1.oids.get(content['contentType']) is not asn1.pkcs7.SignedData:
            raise AuthenticodeParseError("ContentInfo does not contain SignedData")

        data = _guarded_ber_decode(content['content'], asn1_spec=asn1.pkcs7.SignedData())

        signed_data = SignedData(data, *args, **kwargs)
        signed_data._rest_data = rest
        return signed_data

    def _parse(self):
        # Parse the fields of the SignedData structure
        if self.data['version'] != 1:
            raise AuthenticodeParseError("SignedData.version must be 1, not %d" % self.data['version'])

        # digestAlgorithms
        if len(self.data['digestAlgorithms']) != 1:
            raise AuthenticodeParseError("SignedData.digestAlgorithms must contain exactly 1 algorithm, not %d" %
                                         len(self.data['digestAlgorithms']))
        self.digest_algorithm = _get_digest_algorithm(self.data['digestAlgorithms'][0], "SignedData.digestAlgorithm")

        # SpcIndirectDataContent
        self.content_type = asn1.oids.get(self.data['contentInfo']['contentType'])
        if self.content_type is not asn1.spc.SpcIndirectDataContent:
            raise AuthenticodeParseError("SignedData.contentInfo does not contain SpcIndirectDataContent")
        spc_info = _guarded_ber_decode(self.data['contentInfo']['content'], asn1_spec=asn1.spc.SpcIndirectDataContent())
        self.spc_info = SpcInfo(spc_info, signed_data=self)

        # Certificates
        self.certificates = CertificateStore([Certificate(cert) for cert in self.data['certificates']])

        # signerInfos
        if len(self.data['signerInfos']) != 1:
            raise AuthenticodeParseError("SignedData.signerInfos must contain exactly 1 signer, not %d" %
                                         len(self.data['signerInfos']))

        self.signer_info = SignerInfo(self.data['signerInfos'][0])

        # CRLs
        if 'crls' in self.data and self.data['crls'].isValue:
            raise AuthenticodeParseError("SignedData.crls is present, but that is unexpected.")

    def verify(self, expected_hash=None, certificate_context=None, cs_certificate_context=None):
        # Check that the digest algorithms match
        if self.digest_algorithm != self.spc_info.digest_algorithm:
            raise AuthenticodeVerificationError("SignedData.digestAlgorithm must equal SpcInfo.digestAlgorithm")

        if self.digest_algorithm != self.signer_info.digest_algorithm:
            raise AuthenticodeVerificationError("SignedData.digestAlgorithm must equal SignerInfo.digestAlgorithm")

        # Check that the hashes are correct
        # 1. The hash of the file
        if expected_hash is None:
            fingerprinter = self.pefile.get_fingerprinter()
            fingerprinter.add_authenticode_hashers(self.digest_algorithm)
            expected_hash = fingerprinter.hash()[self.digest_algorithm().name]

        if expected_hash != self.spc_info.digest:
            raise AuthenticodeVerificationError("The expected hash does not match the digest in SpcInfo")

        # 2. The hash of the spc blob
        # According to RFC2315, 9.3, identifier (tag) and length need to be
        # stripped for hashing. We do this by having the parser just strip
        # out the SEQUENCE part of the spcIndirectData.
        # Alternatively this could be done by re-encoding and concatenating
        # the individual elements in spc_value, I _think_.
        _, hashable_spc_blob = ber_decoder.decode(self.data['contentInfo']['content'], recursiveFlag=0)
        spc_blob_hash = self.digest_algorithm(bytes(hashable_spc_blob)).digest()
        if spc_blob_hash != self.signer_info.message_digest:
            raise AuthenticodeVerificationError('The expected hash of the SpcInfo does not match SignerInfo')

        # TODO:
        # Can't check authAttr hash against encrypted hash, done implicitly in
        # M2's pubkey.verify. This can be added by explicit decryption of
        # encryptedDigest, if really needed. (See sample code for RSA in
        # 'verbose_authenticode_sig.py')

        if self.signer_info.countersigner:
            auth_attr_hash = self.digest_algorithm(self.signer_info.encrypted_digest).digest()
            if auth_attr_hash != self.signer_info.countersigner.message_digest:
                raise AuthenticodeVerificationError('The expected hash of the encryptedDigest does not match '
                                                    'countersigner\'s SignerInfo')

        if certificate_context is None:
            certificate_context = CertificateVerificationContext(trusted_certificate_store, self.certificates,
                                                                 extended_key_usage=asn1.oids.EKU_CODE_SIGNING)

        if self.signer_info.countersigner:
            if cs_certificate_context is None:
                cs_certificate_context = CertificateVerificationContext(trusted_certificate_store, self.certificates,
                                                                        extended_key_usage=asn1.oids.EKU_TIME_STAMPING)
            cs_certificate_context.timestamp = self.signer_info.countersigner.signing_time

            self.signer_info.countersigner.verify(cs_certificate_context)

            # TODO: What to do when the verification fails? Check it as if the countersignature is not present?
            # Or fail all together? (Which is done now)
            certificate_context.timestamp = self.signer_info.countersigner.signing_time

        self.signer_info.verify(certificate_context)
