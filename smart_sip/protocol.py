"""
SIP Protocol Implementation for Smart SIP Engine
Handles SIP message parsing, building, and transactions
"""

import random
import hashlib
import time
import re
from dataclasses import dataclass
from typing import Dict, Optional, Tuple, List
from enum import Enum
import logging

logger = logging.getLogger(__name__)


class SIPMethod(Enum):
    """SIP request methods"""
    REGISTER = "REGISTER"
    INVITE = "INVITE"
    ACK = "ACK"
    BYE = "BYE"
    CANCEL = "CANCEL"
    OPTIONS = "OPTIONS"
    INFO = "INFO"
    UPDATE = "UPDATE"
    PRACK = "PRACK"


@dataclass
class SIPMessage:
    """Parsed SIP message"""
    is_request: bool
    method: Optional[str] = None       # For requests
    uri: Optional[str] = None          # For requests
    status_code: Optional[int] = None  # For responses
    reason: Optional[str] = None       # For responses
    headers: Dict[str, str] = None
    body: str = ""
    raw: str = ""
    
    def __post_init__(self):
        if self.headers is None:
            self.headers = {}
    
    @property
    def call_id(self) -> str:
        return self.headers.get("Call-ID", "")
    
    @property
    def from_header(self) -> str:
        return self.headers.get("From", "")
    
    @property
    def to_header(self) -> str:
        return self.headers.get("To", "")
    
    @property
    def via(self) -> str:
        return self.headers.get("Via", "")
    
    @property
    def cseq(self) -> Tuple[int, str]:
        cseq = self.headers.get("CSeq", "1 REGISTER")
        parts = cseq.split()
        return int(parts[0]), parts[1] if len(parts) > 1 else ""
    
    @property
    def content_type(self) -> str:
        return self.headers.get("Content-Type", "")


class SIPParser:
    """Parse SIP messages from raw data"""
    
    @staticmethod
    def parse(data: bytes) -> Optional[SIPMessage]:
        """Parse raw bytes into SIPMessage"""
        try:
            text = data.decode('utf-8', errors='ignore')
            lines = text.replace('\r\n', '\n').split('\n')
            
            if not lines:
                return None
            
            # Parse first line (request or response)
            first_line = lines[0].strip()
            
            if first_line.startswith('SIP/2.0'):
                # Response: SIP/2.0 200 OK
                parts = first_line.split(' ', 2)
                msg = SIPMessage(
                    is_request=False,
                    status_code=int(parts[1]) if len(parts) > 1 else 0,
                    reason=parts[2] if len(parts) > 2 else "",
                    raw=text
                )
            else:
                # Request: INVITE sip:user@host SIP/2.0
                parts = first_line.split(' ')
                msg = SIPMessage(
                    is_request=True,
                    method=parts[0] if parts else "",
                    uri=parts[1] if len(parts) > 1 else "",
                    raw=text
                )
            
            # Parse headers
            # Multi-value headers (Via, Record-Route, Contact) can appear multiple times.
            # Accumulate them with comma separation per RFC 3261 §7.3.1.
            _MULTI_VALUE_HEADERS = {"Via", "Record-Route", "Route", "Contact", "WWW-Authenticate", "Proxy-Authenticate"}
            i = 1
            while i < len(lines):
                line = lines[i].strip()
                if not line:  # Empty line = end of headers
                    i += 1
                    break
                
                if ':' in line:
                    key, value = line.split(':', 1)
                    key = key.strip()
                    value = value.strip()
                    if key in _MULTI_VALUE_HEADERS and key in msg.headers:
                        # Append to existing value with comma separator
                        msg.headers[key] = msg.headers[key] + ", " + value
                    else:
                        msg.headers[key] = value
                i += 1
            
            # Rest is body
            msg.body = '\n'.join(lines[i:])
            
            return msg
            
        except Exception as e:
            logger.error(f"SIP parse error: {e}")
            return None


class SIPBuilder:
    """Build SIP messages"""
    
    def __init__(self, local_ip: str, local_port: int, 
                 server_ip: str, server_port: int,
                 user_agent: str = "SmartSIP/1.0",
                 advertise_ip: str = None, advertise_port: int = None,
                 transport: str = "UDP"):
        self.local_ip = local_ip
        self.local_port = local_port
        self.server_ip = server_ip
        self.server_port = server_port
        self.user_agent = user_agent
        # Transport: "UDP" for port 5060, "TLS" for port 5061
        self.transport = transport.upper()
        # NAT support: what we *advertise* in SIP/SDP (for NAT/STUN).
        self.advertise_ip = advertise_ip or local_ip
        self.advertise_port = advertise_port if advertise_port is not None else local_port

    @property
    def _sip_host(self) -> str:
        """Host to place in Via/Contact (NAT-friendly)."""
        return self.advertise_ip

    @property
    def _sip_port(self) -> int:
        """Port to place in Via/Contact (NAT-friendly, e.g. STUN-mapped)."""
        return self.advertise_port
    
    @staticmethod
    def generate_call_id() -> str:
        """Generate unique Call-ID"""
        return f"{random.randint(100000, 999999)}-{int(time.time())}@smartsip"
    
    @staticmethod
    def generate_tag() -> str:
        """Generate random tag"""
        return f"{random.randint(1000000, 9999999)}"
    
    @staticmethod
    def generate_branch() -> str:
        """Generate Via branch parameter"""
        return f"z9hG4bK{random.randint(10000000, 99999999)}"
    
    def build_request(self, method: str, uri: str, headers: Dict[str, str], 
                      body: str = "") -> str:
        """Build a SIP request"""
        lines = [f"{method} {uri} SIP/2.0"]
        
        # Add Content-Length header to dict before building
        content_length = len(body) if body else 0
        headers_with_content = headers.copy()
        headers_with_content["Content-Length"] = str(content_length)
        
        for key, value in headers_with_content.items():
            lines.append(f"{key}: {value}")
        
        lines.append("")  # Empty line separates headers from body
        if body:
            lines.append(body)
        
        # Join and ensure proper ending: \r\n\r\n for no body, or body followed by nothing
        message = "\r\n".join(lines)
        if not body and not message.endswith("\r\n\r\n"):
            # No body case: should end with blank line (\r\n\r\n)
            message += "\r\n"
        return message
    
    def build_response(self, status_code: int, reason: str,
                       request: SIPMessage, body: str = "") -> str:
        """Build a SIP response"""
        lines = [f"SIP/2.0 {status_code} {reason}"]
        
        # Copy Via, From, To, Call-ID, CSeq from request
        lines.append(f"Via: {request.via}")
        lines.append(f"From: {request.from_header}")
        lines.append(f"To: {request.to_header}")
        lines.append(f"Call-ID: {request.call_id}")
        lines.append(f"CSeq: {request.headers.get('CSeq', '')}")
        
        if body:
            lines.append(f"Content-Length: {len(body)}")
        else:
            lines.append("Content-Length: 0")
        
        lines.append("")  # Empty line separates headers from body
        if body:
            lines.append(body)
        
        # Join and ensure proper ending
        message = "\r\n".join(lines)
        if not body and not message.endswith("\r\n\r\n"):
            message += "\r\n"
        return message
    
    def build_register(self, username: str, domain: str, 
                       call_id: str, cseq: int, 
                       from_tag: str, expires: int = 300,
                       auth_header: str = None,
                       auth_type: int = 401) -> str:
        """Build REGISTER request
        
        Args:
            auth_type: 401 for Authorization header, 407 for Proxy-Authorization header
        """
        branch = self.generate_branch()
        uri = f"sip:{domain}"
        
        headers = {
            "Via": f"SIP/2.0/{self.transport} {self._sip_host}:{self._sip_port};rport;branch={branch}",
            "Max-Forwards": "70",
            "From": f"<sip:{username}@{domain}>;tag={from_tag}",
            "To": f"<sip:{username}@{domain}>",
            "Call-ID": call_id,
            "CSeq": f"{cseq} REGISTER",
            "Contact": f"<sip:{username}@{self._sip_host}:{self._sip_port};transport={self.transport.lower()}>",
            "Expires": str(expires),
            "Allow": "INVITE, ACK, BYE, CANCEL, OPTIONS, INFO",
            "Supported": "path, outbound",
            "User-Agent": self.user_agent,
        }
        
        if auth_header:
            if auth_type == 407:
                headers["Proxy-Authorization"] = auth_header
            else:
                headers["Authorization"] = auth_header
        
        return self.build_request("REGISTER", uri, headers)
    
    def build_invite(self, from_user: str, to_number: str, domain: str,
                     call_id: str, cseq: int, from_tag: str,
                     local_rtp_port: int, caller_id: str = None,
                     auth_header: str = None, to_tag: str = None,
                     auth_type: str = "proxy", route: str = None) -> str:
        """Build INVITE request with SDP
        
        Args:
            auth_type: "proxy" for 407 responses (Proxy-Authorization header),
                      "www" for 401 responses (Authorization header)
            route: Record-Route value from 200 OK dialog (reversed for Route header,
                   required for mid-dialog re-INVITEs per RFC 3261 §12.2)
        """
        branch = self.generate_branch()
        uri = f"sip:{to_number}@{domain}"
        
        # Use SIP username in From URI (required by voip.ms and standard SIP auth).
        # Display name can be caller_id for Caller-ID presentation.
        display_name = caller_id if caller_id else from_user
        from_header_val = f"\"{display_name}\" <sip:{from_user}@{domain}>;tag={from_tag}"
        
        # For re-INVITE, include to-tag
        to_header = f"<sip:{to_number}@{domain}>"
        if to_tag:
            to_header += f";tag={to_tag}"
        
        headers = {
            "Via": f"SIP/2.0/{self.transport} {self._sip_host}:{self._sip_port};rport;branch={branch}",
            "Max-Forwards": "70",
            "From": from_header_val,
            "To": to_header,
            "Call-ID": call_id,
            "CSeq": f"{cseq} INVITE",
            "Contact": f"<sip:{from_user}@{self._sip_host}:{self._sip_port}>",
            "Allow": "INVITE, ACK, BYE, CANCEL, OPTIONS, INFO",
            "Supported": "replaces, timer",
            "User-Agent": self.user_agent,
            "Content-Type": "application/sdp",
        }
        
        # Add authorization header if provided
        if auth_header:
            # 407 Proxy Authentication Required → Proxy-Authorization header
            # 401 Unauthorized → Authorization header
            if auth_type == "www" or auth_type == 401:
                headers["Authorization"] = auth_header
            else:
                headers["Proxy-Authorization"] = auth_header

        # Add Route header for mid-dialog requests (re-INVITE)
        # Record-Route from 200 OK must be reversed and used as Route (RFC 3261 §12.2)
        if route:
            headers["Route"] = route

        # Build SDP body
        sdp = self.build_sdp(local_rtp_port)
        
        # Log the SDP to verify G.722 is included
        import logging
        logger = logging.getLogger(__name__)
        logger.info(f"📤 SDP offer includes codecs: {sdp.split('m=audio')[1].split()[2:6] if 'm=audio' in sdp else 'unknown'}")
        logger.info(f"📤 Full SDP:\n{sdp}")
        
        return self.build_request("INVITE", uri, headers, sdp)
    
    def build_sdp(self, rtp_port: int, codecs: List[int] = None) -> str:
        """Build SDP for audio offer/answer"""
        if codecs is None:
            # G.722 FIRST = prefer receiving wideband audio (16kHz, better quality).
            # Provider will select G.722 for RECEIVE path.
            # We always SEND as PCMU (G.711) for compatibility (see RTPStream.send_codec).
            codecs = [9, 0, 8, 101]  # G.722 preferred, then PCMU, PCMA, telephone-event
        
        session_id = int(time.time())
        
        lines = [
            "v=0",
            f"o=- {session_id} {session_id} IN IP4 {self.advertise_ip}",
            "s=SmartSIP Call",
            f"c=IN IP4 {self.advertise_ip}",
            "t=0 0",
            f"m=audio {rtp_port} RTP/AVP {' '.join(map(str, codecs))}",
            "a=ptime:20",
            "a=sendrecv",
        ]

        # Add rtpmap/fmtp attributes only for codecs we advertise
        if 9 in codecs:
            lines.append("a=rtpmap:9 G722/8000")  # clock rate is 8000 in RTP
        if 0 in codecs:
            lines.append("a=rtpmap:0 PCMU/8000")
        if 8 in codecs:
            lines.append("a=rtpmap:8 PCMA/8000")
        if 101 in codecs:
            lines.append("a=rtpmap:101 telephone-event/8000")
            lines.append("a=fmtp:101 0-16")
        
        return "\r\n".join(lines)
    
    def build_ack(self, invite_msg: SIPMessage, to_tag: str = "", contact_uri: str = None, cseq_num: int = None, route: str = None) -> str:
        """Build ACK for INVITE
        
        For 2xx responses, the ACK MUST be sent to the Contact URI from the response (RFC 3261).
        The CSeq MUST match the INVITE that was answered (from the 200 OK response).
        Route header from Record-Route must be included for proxy routing.
        """
        branch = self.generate_branch()
        
        to_header = invite_msg.to_header
        if to_tag and ";tag=" not in to_header:
            to_header = f"{to_header};tag={to_tag}"
        
        # Use contact_uri if provided (from 200 OK), otherwise fall back to original URI
        ack_uri = contact_uri if contact_uri else invite_msg.uri
        
        # Use provided cseq_num (from 200 OK) or fall back to invite's cseq
        ack_cseq = cseq_num if cseq_num is not None else invite_msg.cseq[0]
        
        headers = {
            "Via": f"SIP/2.0/{self.transport} {self._sip_host}:{self._sip_port};rport;branch={branch}",
            "Max-Forwards": "70",
            "From": invite_msg.from_header,
            "To": to_header,
            "Call-ID": invite_msg.call_id,
            "CSeq": f"{ack_cseq} ACK",
            "Contact": f"<sip:{self._sip_host}:{self._sip_port}>",
            "User-Agent": self.user_agent,
        }
        
        # Add Route header if provided (from Record-Route in 200 OK, required for proxy routing)
        if route:
            headers["Route"] = route
        
        return self.build_request("ACK", ack_uri, headers)
    
    def build_bye(self, call_id: str, from_header: str, to_header: str,
                  cseq: int, to_uri: str, contact: str = None, route: str = None) -> str:
        """Build BYE request
        
        Args:
            contact: Optional Contact header URI (sip:user@host:port)
            route: Optional Route header from Record-Route (for proxy routing)
        """
        branch = self.generate_branch()
        
        headers = {
            "Via": f"SIP/2.0/{self.transport} {self._sip_host}:{self._sip_port};rport;branch={branch}",
            "Max-Forwards": "70",
            "From": from_header,
            "To": to_header,
            "Call-ID": call_id,
            "CSeq": f"{cseq} BYE",
            "User-Agent": self.user_agent,
        }
        
        # Add Route header if provided (from Record-Route in 200 OK)
        if route:
            headers["Route"] = route
        
        # Add Contact header if provided (required by some proxies)
        if contact:
            headers["Contact"] = contact
        
        return self.build_request("BYE", to_uri, headers)

    def build_cancel(self, invite_msg: SIPMessage, cseq_num: int) -> str:
        """Build CANCEL to abort in-progress INVITE.
        
        Per RFC 3261 Section 9.1:
        - CANCEL MUST have the same branch as the original INVITE Via
        - CANCEL MUST have the same CSeq number as the INVITE (with method CANCEL)
        - CANCEL MUST have the same Request-URI, Call-ID, To, From as the INVITE
        """
        # MUST reuse the branch from the original INVITE's Via header
        via_header = invite_msg.headers.get("Via", "")
        branch_match = re.search(r'branch=(z9hG4bK[^\s;,]+)', via_header)
        if branch_match:
            branch = branch_match.group(1)
        else:
            branch = self.generate_branch()
        
        uri = invite_msg.uri
        headers = {
            "Via": f"SIP/2.0/{self.transport} {self._sip_host}:{self._sip_port};rport;branch={branch}",
            "Max-Forwards": "70",
            "From": invite_msg.from_header,
            "To": invite_msg.to_header,
            "Call-ID": invite_msg.call_id,
            "CSeq": f"{cseq_num} CANCEL",
            "User-Agent": self.user_agent,
        }
        return self.build_request("CANCEL", uri, headers)


class DigestAuth:
    """SIP Digest Authentication"""
    
    @staticmethod
    def parse_challenge(www_auth: str) -> Dict[str, str]:
        """Parse WWW-Authenticate header"""
        result = {}
        # Remove "Digest " prefix
        if www_auth.lower().startswith("digest "):
            www_auth = www_auth[7:]
        
        # Parse key="value" or key=value pairs
        pattern = r'(\w+)=(?:"([^"]+)"|([^,\s]+))'
        for match in re.finditer(pattern, www_auth):
            key = match.group(1).lower()
            value = match.group(2) if match.group(2) else match.group(3)
            result[key] = value
        
        return result
    
    @staticmethod
    def build_response(username: str, password: str, realm: str,
                       nonce: str, uri: str, method: str,
                       algorithm: str = "MD5", qop: str = None, nc: str = "00000001", cnonce: str = None, opaque: str = None) -> str:
        """Build Authorization header value"""
        
        def md5(s: str) -> str:
            return hashlib.md5(s.encode()).hexdigest()
        
        # Generate cnonce if not provided and qop is used
        if qop and not cnonce:
            cnonce = hashlib.md5(str(time.time()).encode()).hexdigest()[:16]
        
        ha1 = md5(f"{username}:{realm}:{password}")
        ha2 = md5(f"{method}:{uri}")
        
        logger.info(f"[DIGEST AUTH] user={username}, realm={realm}, method={method}, uri={uri}")
        logger.info(f"[DIGEST AUTH] qop={qop}, nonce={nonce[:20]}..., nc={nc}, opaque={opaque[:20] if opaque else 'None'}...")
        logger.debug(f"[DIGEST AUTH] HA2={ha2[:16]}...")
        
        # Calculate response based on qop
        if qop:
            response_str = f"{ha1}:{nonce}:{nc}:{cnonce}:{qop}:{ha2}"
            response = md5(response_str)
            logger.debug(f"[DIGEST AUTH] response hash: {response[:16]}...")
            auth_header = (
                f'Digest username="{username}", realm="{realm}", '
                f'nonce="{nonce}", uri="{uri}", qop={qop}, nc={nc}, '
                f'cnonce="{cnonce}", response="{response}", '
                f'algorithm={algorithm}'
            )
            if opaque:
                auth_header += f', opaque="{opaque}"'
            return auth_header
        else:
            response = md5(f"{ha1}:{nonce}:{ha2}")
            logger.debug(f"[DIGEST AUTH] response hash: {response[:16]}...")
            auth_header = (
                f'Digest username="{username}", realm="{realm}", '
                f'nonce="{nonce}", uri="{uri}", response="{response}", '
                f'algorithm={algorithm}'
            )
            if opaque:
                auth_header += f', opaque="{opaque}"'
            return auth_header


def parse_sdp(sdp: str) -> Dict:
    """Parse SDP to extract RTP info"""
    result = {
        "ip": None,
        "port": None,
        "codecs": [],
    }
    
    for line in sdp.split('\n'):
        line = line.strip()
        
        if line.startswith('c=IN IP4 '):
            result["ip"] = line.split()[-1]
        
        elif line.startswith('m=audio '):
            parts = line.split()
            if len(parts) >= 2:
                result["port"] = int(parts[1])
                result["codecs"] = [int(x) for x in parts[3:] if x.isdigit()]
    
    return result
