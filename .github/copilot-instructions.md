# Project rules

Keep this project generic. Do not add organization, customer, account, tenant,
subscription, personal, or production references to source files or reports.
Keep deployment identifiers, credentials, and raw Azure responses in ignored
`.local/` files only. Results must contain only generic lab identifiers.

Every taggable Azure resource must carry `repo=freezer`, `project=freeze-lab`,
and `deployment=freezer-lab`. Verify tags on implicitly created OS disks too.
Only deploy into a new, dedicated resource group after explicit cost approval.
Never connect this lab to existing networks or production resources.

Do not label local process suspension as Hyper-V validation. Never claim that
Suspend-VM emits an Azure Scheduled Event. Report failures as well as latency.
