#!/usr/bin/env python3
"""
setup_certs.py — ProComm certificate setup
==========================================
Run this ONCE on each new Raspberry Pi:

    python3 setup_certs.py

What it does:
  1. Creates a ProComm Root CA (if not already present in certs/)
  2. Generates a server certificate for this Pi signed by that CA
  3. Generates ProComm-Trust.mobileconfig — the iOS profile users install
     once to trust all ProComm Pis on their iPhone (3 taps, 30 seconds)
  4. Prints a summary of what to do next

The Root CA (ca.crt + ca.key) should be kept safe and reused across
all Pi deployments so users only ever install the trust profile once.
Copy certs/ca.key and certs/ca.crt from your first Pi to all others
before running this script, so they all share the same CA.
"""

import os, sys, uuid, datetime, socket, base64, textwrap

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
CERT_DIR   = os.path.join(SCRIPT_DIR, 'certs')
os.makedirs(CERT_DIR, exist_ok=True)

CA_KEY_FILE   = os.path.join(CERT_DIR, 'ca.key')
CA_CERT_FILE  = os.path.join(CERT_DIR, 'ca.crt')
SRV_KEY_FILE  = os.path.join(CERT_DIR, 'key.pem')
SRV_CERT_FILE = os.path.join(CERT_DIR, 'cert.pem')
PROFILE_FILE  = os.path.join(CERT_DIR, 'ProComm-Trust.mobileconfig')

# iOS 13+ max validity = 825 days
CA_DAYS  = 3650   # 10 years
SRV_DAYS = 820

# ── Ensure cryptography library is available ──────────────────────────────────
try:
    from cryptography import x509
    from cryptography.x509.oid import NameOID, ExtendedKeyUsageOID
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
except ImportError:
    print("Installing required library...")
    os.system(f"{sys.executable} -m pip install cryptography --break-system-packages -q")
    from cryptography import x509
    from cryptography.x509.oid import NameOID, ExtendedKeyUsageOID
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec


def load_or_create_ca():
    """Load existing CA or generate a new one."""
    if os.path.exists(CA_KEY_FILE) and os.path.exists(CA_CERT_FILE):
        print("  Found existing CA — reusing it (good, users won't need to re-install trust)")
        with open(CA_KEY_FILE, 'rb') as f:
            ca_key = serialization.load_pem_private_key(f.read(), password=None)
        with open(CA_CERT_FILE, 'rb') as f:
            ca_cert = x509.load_pem_x509_certificate(f.read())
        return ca_key, ca_cert

    print("  Generating new ProComm Root CA...")
    now = datetime.datetime.utcnow()

    ca_key = ec.generate_private_key(ec.SECP256R1())
    ca_name = x509.Name([
        x509.NameAttribute(NameOID.COMMON_NAME,         'ProComm Root CA'),
        x509.NameAttribute(NameOID.ORGANIZATION_NAME,   'ProComm'),
    ])
    ca_cert = (
        x509.CertificateBuilder()
        .subject_name(ca_name)
        .issuer_name(ca_name)
        .public_key(ca_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now)
        .not_valid_after(now + datetime.timedelta(days=CA_DAYS))
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .add_extension(x509.SubjectKeyIdentifier.from_public_key(ca_key.public_key()), critical=False)
        .add_extension(
            x509.KeyUsage(
                digital_signature=True, key_cert_sign=True, crl_sign=True,
                content_commitment=False, key_encipherment=False,
                data_encipherment=False, key_agreement=False,
                encipher_only=False, decipher_only=False,
            ), critical=True
        )
        .sign(ca_key, hashes.SHA256())
    )

    # Save CA
    with open(CA_KEY_FILE, 'wb') as f:
        f.write(ca_key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.TraditionalOpenSSL,
            serialization.NoEncryption()
        ))
    with open(CA_CERT_FILE, 'wb') as f:
        f.write(ca_cert.public_bytes(serialization.Encoding.PEM))

    print("  Root CA generated and saved.")
    return ca_key, ca_cert


def create_server_cert(ca_key, ca_cert):
    """Generate a server certificate for this Pi, signed by the CA."""
    print("  Generating server certificate for this Pi...")
    now = datetime.datetime.utcnow()

    # Collect hostnames and IPs for this Pi
    hostname = socket.gethostname()
    try:
        local_ip = socket.gethostbyname(hostname)
    except Exception:
        local_ip = '127.0.0.1'

    san_entries = [
        x509.DNSName(hostname),
        x509.DNSName(hostname + '.local'),
        x509.DNSName('procomm.local'),
        x509.DNSName('localhost'),
        x509.IPAddress(__import__('ipaddress').ip_address('127.0.0.1')),
    ]
    # Add LAN IP if it looks private
    try:
        ip_obj = __import__('ipaddress').ip_address(local_ip)
        if ip_obj.is_private:
            san_entries.append(x509.IPAddress(ip_obj))
    except Exception:
        pass

    srv_key = ec.generate_private_key(ec.SECP256R1())
    srv_name = x509.Name([
        x509.NameAttribute(NameOID.COMMON_NAME,       'procomm.local'),
        x509.NameAttribute(NameOID.ORGANIZATION_NAME, 'ProComm'),
    ])

    srv_cert = (
        x509.CertificateBuilder()
        .subject_name(srv_name)
        .issuer_name(ca_cert.subject)
        .public_key(srv_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now)
        .not_valid_after(now + datetime.timedelta(days=SRV_DAYS))
        .add_extension(x509.SubjectAlternativeName(san_entries), critical=False)
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(
            x509.KeyUsage(
                digital_signature=True, key_encipherment=True,
                content_commitment=False, data_encipherment=False,
                key_agreement=False, key_cert_sign=False,
                crl_sign=False, encipher_only=False, decipher_only=False,
            ), critical=True
        )
        .add_extension(
            x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]),
            critical=False
        )
        .sign(ca_key, hashes.SHA256())
    )

    with open(SRV_KEY_FILE, 'wb') as f:
        f.write(srv_key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.TraditionalOpenSSL,
            serialization.NoEncryption()
        ))
    with open(SRV_CERT_FILE, 'wb') as f:
        f.write(srv_cert.public_bytes(serialization.Encoding.PEM))

    print(f"  Server cert covers: {', '.join(str(s.value) for s in san_entries)}")


def create_mobileconfig(ca_cert):
    """Generate an Apple Configuration Profile to trust the ProComm CA on iOS."""
    print("  Generating ProComm-Trust.mobileconfig for iPhone...")

    # CA cert in DER format, base64 encoded for the profile
    ca_der = ca_cert.public_bytes(serialization.Encoding.DER)
    ca_b64 = base64.b64encode(ca_der).decode()
    # Wrap at 68 chars for readability inside the plist
    ca_b64_wrapped = '\n\t\t\t\t'.join(textwrap.wrap(ca_b64, 68))

    profile_uuid = str(uuid.uuid4()).upper()
    payload_uuid = str(uuid.uuid4()).upper()

    profile = f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>PayloadContent</key>
    <array>
        <dict>
            <key>PayloadCertificateFileName</key>
            <string>ProComm-CA.crt</string>
            <key>PayloadContent</key>
            <data>
                {ca_b64_wrapped}
            </data>
            <key>PayloadDescription</key>
            <string>Allows your iPhone to trust ProComm's local phone system</string>
            <key>PayloadDisplayName</key>
            <string>ProComm Phone Trust</string>
            <key>PayloadIdentifier</key>
            <string>com.procomm.ca.cert</string>
            <key>PayloadType</key>
            <string>com.apple.security.root</string>
            <key>PayloadUUID</key>
            <string>{payload_uuid}</string>
            <key>PayloadVersion</key>
            <integer>1</integer>
        </dict>
    </array>
    <key>PayloadDescription</key>
    <string>Installs trust for the ProComm local phone system. After installing, the phone keypad opens instantly with no security warnings.</string>
    <key>PayloadDisplayName</key>
    <string>ProComm Phone Trust</string>
    <key>PayloadIdentifier</key>
    <string>com.procomm.trust.profile</string>
    <key>PayloadOrganization</key>
    <string>ProComm</string>
    <key>PayloadRemovalDisallowed</key>
    <false/>
    <key>PayloadType</key>
    <string>Configuration</string>
    <key>PayloadUUID</key>
    <string>{profile_uuid}</string>
    <key>PayloadVersion</key>
    <integer>1</integer>
</dict>
</plist>"""

    with open(PROFILE_FILE, 'w') as f:
        f.write(profile)
    print("  ProComm-Trust.mobileconfig saved.")


def main():
    print()
    print("═══════════════════════════════════════════")
    print("  ProComm Certificate Setup")
    print("═══════════════════════════════════════════")
    print()

    ca_key, ca_cert = load_or_create_ca()
    create_server_cert(ca_key, ca_cert)
    create_mobileconfig(ca_cert)

    print()
    print("═══════════════════════════════════════════")
    print("  Done! Files in ./certs/:")
    print("    cert.pem              — server certificate")
    print("    key.pem               — server private key")
    print("    ca.crt                — Root CA")
    print("    ProComm-Trust.mobileconfig — iPhone trust profile")
    print()
    print("  Next steps:")
    print()
    print("  1. Restart ProComm so it loads the new cert.")
    print()
    print("  2. Each iPhone needs to install the trust profile ONCE:")
    print("     • Open Safari on iPhone")
    print("     • Go to http://procomm.local:5000/trust")
    print("     • Tap 'Allow' → go to Settings → 'Profile Downloaded'")
    print("     • Tap Install → enter passcode → tap Trust → Done")
    print()
    print("  3. After that, scanning the QR opens the phone")
    print("     instantly — no warnings, ever, on any ProComm Pi.")
    print()
    print("  For new Pis: copy certs/ca.key + certs/ca.crt to the")
    print("  new Pi first, then run this script. Same CA = no new")
    print("  trust install needed on iPhones.")
    print("═══════════════════════════════════════════")
    print()


if __name__ == '__main__':
    main()
