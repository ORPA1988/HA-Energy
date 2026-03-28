ARG BUILD_FROM
FROM ${BUILD_FROM}

RUN apk add --no-cache python3 py3-pip py3-requests py3-flask tzdata

WORKDIR /app

COPY src/ /app/energieha/
COPY run.sh /

RUN chmod a+x /run.sh

CMD [ "/run.sh" ]
