ARG BUILD_FROM
FROM ${BUILD_FROM}

RUN apk add --no-cache python3 py3-pip py3-requests tzdata \
    && pip3 install --no-cache-dir --break-system-packages flask

WORKDIR /app

COPY src/ /app/energieha/
COPY run.sh /

RUN chmod a+x /run.sh

CMD [ "/run.sh" ]
