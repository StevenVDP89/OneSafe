// Template for app/dist/config.js.
//
// The real file is written by `python tools/setup.py` from tools/config.json —
// it is generated, not edited, and is gitignored so one person's tenant does
// not follow the repo around.
//
// Nothing here is secret. clientId is a public SPA registration; the app holds
// no credential of its own and queries the model strictly as the signed-in
// user, so an admin sees exactly what their own rights allow.
window.CONFIG = {
  tenantId: "00000000-0000-0000-0000-000000000000",
  clientId: "00000000-0000-0000-0000-000000000000",
  datasetId: "00000000-0000-0000-0000-000000000000",
  workspaceId: "00000000-0000-0000-0000-000000000000",
  pbiScopes: ["https://analysis.windows.net/powerbi/api/Dataset.Read.All"],
  fabricBase: "https://app.fabric.microsoft.com",
};
