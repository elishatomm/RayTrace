import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { App } from "../../src/App";
import "../../src/styles.css";
import "./demo.css";
import { DemoBanner, DemoGuide } from "./DemoBanner";
import { installReplay } from "./replay";

// Before the App renders: from here on every request the App sends is answered by the replay, and none leaves the page.
const replay = installReplay(window);

const root = document.getElementById("root");
if (root === null) {
  throw new Error("index.html has no #root element to render the recorded demo into");
}
createRoot(root).render(
  <StrictMode>
    <DemoBanner replay={replay} />
    <DemoGuide replay={replay} />
    <App />
  </StrictMode>,
);
