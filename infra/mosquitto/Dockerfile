# Mosquitto broker image with the lab configuration baked in, so the stack does not
# depend on bind mounts (host and Dev Container paths may differ).
FROM eclipse-mosquitto:2.1.2-alpine

COPY mosquitto.conf /mosquitto/config/mosquitto.conf
