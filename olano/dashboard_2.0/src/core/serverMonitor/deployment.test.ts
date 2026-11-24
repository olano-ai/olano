import assert from "node:assert/strict";
import test from "node:test";

import { consoleLink, consoleSecurityLink, slugFromHost } from "./deployment.ts";

test("the Security tab link is the console's deep link for that deployment", () => {
  assert.equal(
    consoleSecurityLink("0b6f7c1e-3a2d-4c8e-9f10-2d5b7e8a9c41"),
    "https://cloud.olano.ai/app/deployments/0b6f7c1e-3a2d-4c8e-9f10-2d5b7e8a9c41?tab=security",
  );
});

test("an id can never add a path segment or a query of its own", () => {
  // The id comes from a root-owned manifest, but a link built from data should
  // not depend on that data being well-formed.
  assert.equal(
    consoleSecurityLink("../x?tab=danger"),
    "https://cloud.olano.ai/app/deployments/..%2Fx%3Ftab%3Ddanger?tab=security",
  );
});

test("the hostname helpers are unchanged", () => {
  assert.equal(slugFromHost("agents.olano.run"), "agents");
  assert.equal(slugFromHost("a.b.olano.run"), null);
  assert.equal(consoleLink("agents.olano.run"), "https://cloud.olano.ai/app/deployments/agents");
  assert.equal(consoleLink("10.0.0.7"), "https://cloud.olano.ai/app/deployments");
});
