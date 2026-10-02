# Demo Test Report

## Validated demo flow

The following real backend workflow was validated against the live API using seeded demo credentials:

1. Citizen login
2. Complaint submission for `TS09AB1234`
3. Cop login
4. Complaint verification
5. Hotlist creation
6. Volunteer login and device registration
7. Detection submission for the same plate
8. Hotlist match
9. Sighting saved

## Evidence

The live validation script printed:

```
{
  "complaint_id": "6d974430-16b2-489e-9ab6-0e5e85d71362",
  "hotlist_id": "6d1fa11f-fa3f-49cd-bec4-a9c630412422",
  "device_id": "aca9e218-96c8-4af8-9968-388823cf079a",
  "sighting_id": "78c0e2d8-7d12-4806-a142-04b2c821457e",
  "plate": "TS09AB1234",
  "status": "MATCHED",
  "hotlist_count": 5,
  "alert_count": 25
}
```

## Final demo status

- PASS: backend acceptance flow works and creates the hotlist match.
- PASS: citizen → police → hotlist → mobile detection flow is validated at the API level.
- PARTIAL: the real browser and phone UI around the live dashboard alert and device capture still need manual run-through on the connected handset.
- SIMULATED: live WebSocket UI alerting was not observed in a browser-connected session in this environment.

## Recommendation

Use the live backend plus the open phone app and the dashboard pages to manually confirm the final demo narrative in a demo session. The API foundation is already validated and working.
