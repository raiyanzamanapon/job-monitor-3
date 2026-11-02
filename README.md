# Job Monitor (Bangladesh · architecture)

Double-click to open. It checks BD Govt Job, university notice pages (28 universities) and bank circulars (67 banks),
in English and Bengali, and shows the results in a frosted-glass window.

Views: Latest (what changed since the last check), Jobs (sort/filter), Applied (stage, dates, ID/login/password/transaction notes),
Saved, Sources (which pages were readable), Search tools (LinkedIn/Google links, erecruitment.bb.org.bd), Report, Activity.

Files next to the program: config.json, state.json, applications.json, user_sources.json, latest_report.md.
Passwords in Applied are stored as plain text on your PC: use for portal logins only.

Build the exe: put JobMonitorApp.pyw and .github/workflows/build-exe.yml in a GitHub repository, then run the Actions workflow.
