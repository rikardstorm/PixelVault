# Contributing

Run `py -m unittest discover -s tests -v` before submitting changes.
Keep failures conservative: an uncertain upload must never justify deletion.
Add meaningful tests for account/quality guards, journal recovery and transfer
verification when changing those paths. Use synthetic filenames and accounts.

For UI compatibility reports, describe the device, Android version, Photos
version, English button labels and reproduction steps. Redact screenshots and
logs before posting. Do not publish archives, real account details or PIN data.
An actual-device test is required before claiming a new platform/UI is supported.
