#!/usr/bin/env python
# coding=utf-8

import grpc
from datetime import datetime, timezone
import sys, netns, time
import logging
import socket
import os, re
import signal
import traceback
import json

import requests, urllib3, pynetbox

import sdk_service_pb2
import sdk_service_pb2_grpc
import config_service_pb2

from pygnmi.client import gNMIclient

from logging.handlers import RotatingFileHandler

############################################################
## Agent will start with this name
############################################################
agent_name='netbox_agent'

####
# Set global HTTP retry strategy
####
from requests.adapters import HTTPAdapter
from requests.packages.urllib3.util.retry import Retry

retry_strategy = Retry(
    total=3,backoff_factor=1,
    status_forcelist=[429, 500, 502, 503, 504],
    method_whitelist=["HEAD", "GET", "OPTIONS", "POST"]
)
adapter = HTTPAdapter(max_retries=retry_strategy)
http = requests.Session()
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
http.verify = False  # Disable SSL verify
http.mount("https://", adapter)
http.mount("http://", adapter)

############################################################
## Open a GRPC channel to connect to sdk_mgr on the dut
## sdk_mgr will be listening on 50053
############################################################
#channel = grpc.insecure_channel('unix:///opt/srlinux/var/run/sr_sdk_service_manager:50053')
channel = grpc.insecure_channel('127.0.0.1:50053')
metadata = [('agent_name', agent_name)]
stub = sdk_service_pb2_grpc.SdkMgrServiceStub(channel)

# Global gNMI channel, used by multiple threads
#gnmi_options = [('username', 'admin'), ('password', 'admin')]
#gnmi_channel = grpc.insecure_channel(
#   'unix:///opt/srlinux/var/run/sr_gnmi_server', options = gnmi_options )

############################################################
## Subscribe to required event
## This proc handles subscription of: Config
############################################################
def Subscribe(stream_id, option):
    op = sdk_service_pb2.NotificationRegisterRequest.AddSubscription
    if option == 'cfg':
        entry = config_service_pb2.ConfigSubscriptionRequest()
        # entry.key.js_path = '.' + agent_name
        request = sdk_service_pb2.NotificationRegisterRequest(op=op, stream_id=stream_id, config=entry)

    subscription_response = stub.NotificationRegister(request=request, metadata=metadata)
    print('Status of subscription response for {}:: {}'.format(option, subscription_response.status))

############################################################
## Subscribe to all the events that Agent needs
############################################################
def Subscribe_Notifications(stream_id):
    '''
    Agent will receive notifications to what is subscribed here.
    '''
    if not stream_id:
        logging.info("Stream ID not sent.")
        return False

    # Subscribe to config changes, first
    Subscribe(stream_id, 'cfg')

##################################################################
## Proc to process the config Notifications received by auto_config_agent
## At present processing config from js_path containing agent_name
##################################################################
def Handle_Notification(obj, state):
    if obj.HasField('config'):
        logging.info(f"GOT CONFIG :: {obj.config.key.js_path}")
        if agent_name in obj.config.key.js_path:
            logging.info(f"Got config for agent, now will handle it :: \n{obj.config}\
                            Operation :: {obj.config.op}\nData :: {obj.config.data.json}")
            if obj.config.op == 2:
                logging.info(f"Delete netbox-agent cli scenario")
                response=stub.AgentUnRegister(request=sdk_service_pb2.AgentRegistrationRequest(), metadata=metadata)
                logging.info( f'Handle_Config: Unregister response:: {response}' )
                state = State() # Reset state, works?
            else:
                # Don't replace ' in filter expressions
                json_acceptable_string = obj.config.data.json # .replace("'", "\"")
                data = json.loads(json_acceptable_string)

                if 'netbox_url' in data:
                   state.netbox_url = data['netbox_url']['value']
                if 'netbox_token' in data:
                   state.netbox_token = data['netbox_token']['value']
                if 'netbox_user' in data:
                   state.netbox_user = data['netbox_user']['value']
                if 'netbox_password' in data:
                   state.netbox_password = data['netbox_password']['value']
                return True
        elif obj.config.key.js_path == ".commit.end":
           logging.info( "Connect to Netbox and commit" )
           try:
              RegisterWithNetbox(state)
           except Exception as e:
              logging.error(e)
    else:
        logging.info(f"Unexpected notification : {obj}")

    # dont subscribe to LLDP now
    return False

#
# Uses gNMI to get /platform/chassis/mac-address and format as hhhh.hhhh.hhhh
#
def GetSystemMAC():
   path = '/platform/chassis/mac-address'
   with gNMIclient(target=('unix:///opt/srlinux/var/run/sr_gnmi_server',57400),
                            username="admin",password="admin",
                            insecure=True, debug=False) as gnmi:
      result = gnmi.get( encoding='json_ietf', path=[path] )
      for e in result['notification']:
         if 'update' in e:
           logging.info(f"GetSystemMAC GOT Update :: {e['update']}")
           m = e['update'][0]['val'] # aa:bb:cc:dd:ee:ff
           return f'{m[0]}{m[1]}.{m[2]}{m[3]}.{m[4]}{m[5]}'

   return "0000.0000.0000"

def GetNetboxToken(state):
    logging.info(f"GetNetboxToken...state={state}")
    if state.netbox_token != "":
       return state.netbox_token
    try:
      requests_log = logging.getLogger("requests.packages.urllib3")
      requests_log.setLevel(logging.DEBUG)
      requests_log.propagate = True
      # May fail during bootstrap, now set auto-retry with back-off
      response = http.post(f'{state.netbox_url}/api/users/tokens/provision/',
                           json={ "username": state.netbox_user,
                                  "password": state.netbox_password },
                           timeout=5 )
      logging.info(f"GetNetboxToken response:{response}")
      response.raise_for_status() # Throw exception if error
      return response.json()['key']
    except Exception as ex:
      logging.error( ex )
    return None

def RegisterWithNetbox(state):
    # During system startup, wait for netns to be created
    while not os.path.exists('/var/run/netns/srbase-mgmt'):
       logging.info("Waiting for srbase-default netns to be created...")
       time.sleep(1)
    with netns.NetNS(nsname="srbase-mgmt"):
      nb = pynetbox.api( url=state.netbox_url, token=GetNetboxToken(state) )
      nb.http_session = http
      hostname = socket.gethostname()
      logging.info( f"RegisterWithNetbox creating device...{hostname}")
      host_site = re.match( "^(\S+)[.](.*)$", hostname )
      if host_site:
          device_name = host_site.groups()[0]
          device_site = host_site.groups()[1]
      else:
          device_name = hostname
          device_site = "undefined"
      new_chassis = nb.dcim.devices.create(
        name=device_name,
        # See https://github.com/netbox-community/devicetype-library/blob/master/device-types/Nokia/7210-SAS-Sx.yaml
        device_type="7220-ixr-d1-10-100GE",  # Slug, needs to exist in Netbox
        serial=GetSystemMAC(),
        device_role=state.role,     # Needs to exist
        site=device_site,           # Cannot be NULL
        tenant=None,
        rack=None,
        tags=[],
      )
    # TODO use LLDP events to register links

class State(object):
    def __init__(self):
        self.netbox_url = "http://172.20.20.1:8000"
        self.netbox_user = "admin"
        self.netbox_password = "admin"
        self.netbox_token = ""
        self._determine_role()

    def __str__(self):
        return str(self.__class__) + ": " + str(self.__dict__)

    def _determine_role(self):
       """
       Determine this node's role and relative ID based on the hostname
       """
       hostname = socket.gethostname()
       role_id = re.match( "^(\w+)[-]?(\d+).*$", hostname ) # Ignore trailing router ID, if set
       if role_id:
           self.role = role_id.groups()[0]
           self.id_from_hostname = int( role_id.groups()[1] )
           logging.info( f"_determine_role: role={self.role} id={self.id_from_hostname}" )
       else:
           logging.warning( f"_determine_role: Unable to determine role/id based on hostname: {hostname}, switching to 'auto' mode" )
           self.role = "auto"
           self.id_from_hostname = 0

##################################################################################################
## This is the main proc where all processing for Netbox agent starts.
## Agent registration, notification registration, Subscrition to notifications.
## Waits on the subscribed Notifications and once any config is received, handles that config
## If there are critical errors, Unregisters the fib_agent gracefully.
##################################################################################################
def Run():
    # optional agent_liveliness=<seconds> to have system kill unresponsive agents
    response = stub.AgentRegister(request=sdk_service_pb2.AgentRegistrationRequest(), metadata=metadata)
    logging.info(f"Registration response : {response.status}")

    request=sdk_service_pb2.NotificationRegisterRequest(op=sdk_service_pb2.NotificationRegisterRequest.Create)
    create_subscription_response = stub.NotificationRegister(request=request, metadata=metadata)
    stream_id = create_subscription_response.stream_id
    logging.info(f"Create subscription response received. stream_id : {stream_id}")

    Subscribe_Notifications(stream_id)

    sub_stub = sdk_service_pb2_grpc.SdkNotificationServiceStub(channel)
    stream_request = sdk_service_pb2.NotificationStreamRequest(stream_id=stream_id)
    stream_response = sub_stub.NotificationStream(stream_request, metadata=metadata)

    state = State()
    count = 1
    try:
        for r in stream_response:
            logging.info(f"Count :: {count}  NOTIFICATION:: \n{r.notification}")
            count += 1
            for obj in r.notification:
                Handle_Notification(obj, state)
                logging.info(f'Updated state: {state}')

    except grpc._channel._Rendezvous as err:
        logging.info(f'GOING TO EXIT NOW: {err}')

    except Exception as e:
        logging.error(f'Exception caught :: {e}')

    finally:
        Exit_Gracefully(0,0)
    return True

############################################################
## Gracefully handle SIGTERM signal
## When called, will unregister Agent and gracefully exit
############################################################
def Exit_Gracefully(signum, frame):
    logging.info(f"Caught signal :: {signum}\n will unregister netbox agent")
    try:
        response=stub.AgentUnRegister(request=sdk_service_pb2.AgentRegistrationRequest(), metadata=metadata)
        logging.error( f'try: Unregister response:: {response}')
    except grpc._channel._Rendezvous as err:
        logging.info(f'GOING TO EXIT NOW: {err}')
    finally:
        sys.exit()

##################################################################################################
## Main from where the Agent starts
## Log file is written to: /var/log/srlinux/stdout/<agent_name>.log
## Signals handled for graceful exit: SIGTERM
##################################################################################################
if __name__ == '__main__':
    # hostname = socket.gethostname()
    stdout_dir = '/var/log/srlinux/stdout' # PyTEnv.SRL_STDOUT_DIR
    signal.signal(signal.SIGTERM, Exit_Gracefully)
    if not os.path.exists(stdout_dir):
        os.makedirs(stdout_dir, exist_ok=True)
    log_filename = f'{stdout_dir}/{agent_name}.log'
    logging.basicConfig(
      handlers=[RotatingFileHandler(log_filename, maxBytes=3000000,backupCount=5)],
      format='%(asctime)s,%(msecs)03d %(name)s %(levelname)s %(message)s',
      datefmt='%H:%M:%S', level=logging.INFO)
    logging.info("START TIME :: {}".format(datetime.now()))
    if Run():
        logging.info('Netbox agent unregistered')
    else:
        logging.info('Should not happen')
