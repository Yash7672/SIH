# Privacy Test Report

## Core privacy rule

The architecture is designed to send only the minimum required ANPR data to the backend: plate, GPS, timestamp, confidence, and device metadata. Raw video and frame data are not expected to be transmitted.

## Verified privacy contract

### MOBILE → BACKEND

The backend sighting schema accepts the following minimum payload:

- `plate`
- `latitude`
- `longitude`
- `timestamp`
- `confidence`
- `device_id`

This matches the live API contract in [backend/app/schemas/entities.py](../backend/app/schemas/entities.py) and the behaviour enforced in [backend/app/api/v1/sightings.py](../backend/app/api/v1/sightings.py).

### Explicitly not accepted

- raw video uploads
- frame streams
- multipart camera payloads on detection endpoints
- arbitrary image data on the detection endpoint
- complaint proof uploads outside the complaint route

This is enforced in [backend/app/main.py](../backend/app/main.py) with the privacy guard middleware.

## Verified status

- PASS: detection requests are validated against the narrowed schema.
- PASS: the API rejects invalid plate formats safely.
- PASS: the complaint upload route allows file proof only where needed.
- PARTIAL: actual phone camera streaming was not recorded from the physical device in this environment.

## Conclusion

The backend design and live validation align with the privacy requirement: no continuous video upload is expected or accepted. Real on-device camera testing should still be manually confirmed on the connected phone during the final demo run.
