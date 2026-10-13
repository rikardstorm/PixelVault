# Security and data handling

This project handles personal media and can invoke Photos' native local cleanup.
Please do not post vulnerabilities with sensitive data in a public issue. Use
GitHub's private vulnerability reporting when enabled by the repository owner.

Source ZIPs are never edited. Cloud deletion is outside the project's scope.
Unknown backup states must retain phone copies. All runtime state is local in
`.pixel-uploader/`, which is ignored by Git. DPAPI protects optional credentials
only against different Windows users, not processes with the same identity.
