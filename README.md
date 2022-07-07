# Download Integrity Checker for ASF Infra

This service runs as a [Pipservice](https://cwiki.apache.org/confluence/display/INFRA/Pipservices) and 
verifies download artifacts using their accompanying chekcums and detached signatures, as per our 
release distribution policies ( outlined at https://infra.apache.org/release-distribution.html ).

When a mismatch is detected, projects (and infra) are notified of this via email.
