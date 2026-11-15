# The upstream image contains only the server binary and its libraries, with
# no shell. We copy those into a small Alpine image so a start script can read
# the add-on options.
FROM jadedblueeyes/continuwuity:v26.9.4 AS server

# The group-rules bot needs a chat library with end-to-end encryption.
FROM alpine:3.22 AS bot
RUN apk add --no-cache python3 py3-pip build-base
COPY group_bot/requirements.txt /requirements.txt
RUN pip install --no-cache-dir --break-system-packages --target /opt/group_bot/lib \
      -r /requirements.txt

FROM alpine:3.22

RUN apk add --no-cache jq ca-certificates python3 openssl nginx

# Keep the server's own loader and libraries together in one folder so they
# never mix with Alpine's.
COPY --from=server /sbin/conduwuit /opt/continuwuity/conduwuit
COPY --from=server /lib64/ld-linux-x86-64.so.2 /opt/continuwuity/lib/
COPY --from=server /usr/lib/ /opt/continuwuity/lib/

COPY run.sh /run.sh
COPY push_gateway.py /push_gateway.py
COPY invite_service.py /invite_service.py
COPY --from=bot /opt/group_bot/lib /opt/group_bot/lib
COPY group_bot /opt/group_bot/group_bot
RUN chmod 0755 /run.sh

EXPOSE 6167
ENTRYPOINT ["/run.sh"]
