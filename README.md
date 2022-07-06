# Download Integrity Checker for ASF Infra

This service runs as a [Pipservice](https://cwiki.apache.org/confluence/display/INFRA/Pipservices) and 
verifies download artefacts using their accompanying chekcums and detached signatures.

When a mismatch is detected, projects (and infra) is notified of this via email.
