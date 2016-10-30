import collections
import concurrent.futures
import contextlib
import io
import itertools
import socket
import traceback
import urllib

from xml.etree import ElementTree
from xml.etree.ElementTree import Element as E, SubElement as sE, ElementTree as ET, QName

ns = {
    'd': 'urn:schemas-upnp-org:device-1-0',
    's': 'http://schemas.xmlsoap.org/soap/envelope',
    'i': 'urn:schemas-upnp-org:service:WANCommonInterfaceConfig:1',
}

# Any other values causes my IGD to close the connection before sending a
# response.
ElementTree.register_namespace('s', ns['s'])
ElementTree.register_namespace('u', ns['i'])

class Device(collections.namedtuple('Device', ['udn', 'url'])):
    pass

def search(timeout):
    with contextlib.ExitStack() as stack:
        sockets = []
        sockets.append(stack.enter_context(socket.socket(socket.AF_INET6, socket.SOCK_DGRAM, socket.IPPROTO_UDP)))
        sockets.append(stack.enter_context(socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)))

        for s in sockets:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            if s.family == socket.AF_INET6:
                s.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 1)

            with concurrent.futures.ThreadPoolExecutor(len(sockets)) as ex:
                return itertools.chain(*ex.map(lambda s: search_socket(s, timeout, ns['i']), sockets))

def search_socket(sock, timeout, target='upnp:rootdevice'):

    addr = 'ff02::c' if sock.family == socket.AF_INET6 else '239.255.255.250'
    host = '[{}]'.format(addr) if sock.family == socket.AF_INET6 else addr
    msg = b'M-SEARCH * HTTP/1.1\r\n' \
        b'HOST: %s:1900\r\n' \
        b'MAN: "ssdp:discover"\r\n' \
        b'MX: %d\r\n' \
        b'ST: %s\r\n' \
        b'\r\n' \
            % (host.encode('latin1'), timeout, ns['i'].encode('latin1'))
    sock.sendto(msg, (addr, 1900))

    result = []

    for n in range(100):
        sock.settimeout(timeout)
        try:
            buf, addr = sock.recvfrom(1024)
        except socket.timeout:
            break

        try:
            result.append(search_result(buf, addr))
        except:
            traceback.print_exc()

    return result

def search_result(buf, addr):
    try:
        headers, buf = search_parse(buf)
    except:
        raise Exception('Malformed search result from {}'.format(addr))
    else:
        return headers[b'Location'].decode('latin1')

def search_parse(buf):
    status, sep, buf = buf.partition(b'\r\n')
    version, status, reason = status.split()
    if status != b'200':
        raise Exception('Unknown status {}'.format(status))
    headers = {}
    while True:
        header, sep, buf = buf.partition(b'\r\n')
        if header == b'':
            break
        else:
            name, sep, value = header.partition(b':')
            headers[name] = value.lstrip()

    return headers, buf

def probe(target_url):
    device = probe_device(target_url)

    result = []
    with concurrent.futures.ThreadPoolExecutor(4) as ex:
        for metric, value in ex.map(lambda metric: (metric, probe_metric(device.url, metric)), ['TotalBytesReceived', 'TotalBytesSent', 'TotalPacketsReceived', 'TotalPacketsSent']):
            if value < 0:
                # WANCommonInterfaceConfig:1 specifies these values with the
                # 'ui4' data type. Assume any negative values are caused by the
                # IGD formatting the value as a signed 32-bit integer.
                value += 2 ** 32
            result.append(b'igd_WANDevice_1_WANCommonInterfaceConfig_1_%s{udn="%s"} %d\n' % (metric.encode('utf-8'), device.udn.encode('utf-8'), value))

    return result

def probe_device(target_url):
    with urllib.request.urlopen(target_url) as scpd:
        st = ElementTree.parse(scpd)

    url_base = st.findtext('d:URLBase', namespaces=ns)
    device = st.find("d:device[d:deviceType='urn:schemas-upnp-org:device:InternetGatewayDevice:1']/d:deviceList/d:device[d:deviceType='urn:schemas-upnp-org:device:WANDevice:1']", ns)
    url_path = device.findtext("d:serviceList/d:service[d:serviceType='urn:schemas-upnp-org:service:WANCommonInterfaceConfig:1']/d:controlURL", namespaces=ns)

    return Device(device.findtext('d:UDN', namespaces=ns), urllib.parse.urljoin(url_base, url_path))

def probe_metric(service_url, metric):
    envelope = E(QName(ns['s'], 'Envelope'), {QName(ns['s'], 'encodingStyle'): 'http://schemas.xmlsoap.org/soap/encoding/'})
    body = sE(envelope, QName(ns['s'], 'Body'))
    method = sE(body, QName(ns['i'], 'Get{}'.format(metric)))
    request_tree = ET(envelope)
    with io.BytesIO() as out:
        request_tree.write(out, xml_declaration=True)
        req = urllib.request.Request(service_url, out.getvalue())

    req.add_header('Content-Type', 'text/xml')
    req.add_header('SOAPAction', '"{}#{}"'.format(ns['i'], 'Get{}'.format(metric)))

    with urllib.request.urlopen(req) as result:
        result_tree = ElementTree.parse(result)
        return int(result_tree.findtext('.//New{}'.format(metric), namespaces=ns))

if __name__ == '__main__':
    for url in search(5):
        print(url)
