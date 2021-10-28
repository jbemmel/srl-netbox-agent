# ARG SR_LINUX_RELEASE
# FROM srl/custombase:$SR_LINUX_RELEASE AS final
FROM srl/auto-config-v2:latest

RUN sudo pip3 install requests urllib3 pynetbox

RUN sudo mkdir --mode=0755 -p /etc/opt/srlinux/appmgr/
COPY --chown=srlinux:srlinux ./srl-netbox-agent.yml /etc/opt/srlinux/appmgr
COPY ./src /opt/demo-agents/

# Add in auto-config agent sources too
# COPY --from=srl/auto-config-v2:latest /opt/demo-agents/ /opt/demo-agents/

# run pylint to catch any obvious errors
RUN PYTHONPATH=$AGENT_PYTHONPATH pylint --load-plugins=pylint_protobuf -E /opt/demo-agents/srl-netbox-agent

# Using a build arg to set the release tag, set a default for running docker build manually
ARG SRL_NETBOX_AGENT_RELEASE="[custom build]"
ENV SRL_NETBOX_AGENT_RELEASE=$SRL_NETBOX_AGENT_RELEASE
