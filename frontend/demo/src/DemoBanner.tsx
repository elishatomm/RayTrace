import { type RefObject, useEffect, useRef, useSyncExternalStore } from "react";
import type { DemoReplay } from "./replay";

// The banner sticks to the top of every view. The page's own sticky case view and scroll-into-view must stop below
// it, so its height is kept in --demo-banner-height on the root element (demo.css reads it).
function useBannerHeight(banner: RefObject<HTMLElement | null>): void {
  useEffect(() => {
    const element = banner.current;
    if (element === null) {
      return;
    }
    const root = document.documentElement;
    const observer = new ResizeObserver(() => root.style.setProperty("--demo-banner-height", `${element.offsetHeight}px`));
    observer.observe(element);
    return () => {
      observer.disconnect();
      root.style.removeProperty("--demo-banner-height");
    };
  }, [banner]);
}

// Reloading the page starts a fresh replay: every run back at its first stage.
function restart(): void {
  window.location.reload();
}

export function DemoBanner({ replay }: Readonly<{ replay: DemoReplay }>) {
  const notice = useSyncExternalStore(replay.subscribe, replay.notice);
  const banner = useRef<HTMLElement>(null);
  useBannerHeight(banner);
  return (
    <section ref={banner} className="demo-banner" aria-label="Recorded demo">
      <p className="demo-statement">
        <strong>Recorded demo.</strong> Every answer on this page is a recorded response from the real RayTrace
        API in rules mode. Nothing runs here: no provider is called and no money moves. All data is synthetic.
      </p>
      <button type="button" onClick={restart}>Restart the demo</button>
      {notice !== null && (
        <p className="demo-notice">
          {notice}{" "}
          <button type="button" className="link" onClick={replay.dismiss}>Dismiss</button>
        </p>
      )}
    </section>
  );
}

export function DemoGuide({ replay }: Readonly<{ replay: DemoReplay }>) {
  const runs = useSyncExternalStore(replay.subscribe, replay.runs);
  return (
    <details className="demo-guide">
      <summary>What this recorded demo covers</summary>
      {runs === null ? (
        <p>Loading the recording...</p>
      ) : (
        <>
          <ol>
            {runs.map((run) => (
              <li key={run.run_id}>
                <strong>Flow {run.flow}: {run.title}.</strong> Under Open run, choose{" "}
                <code>{run.run_id} ({run.scenario}, seed {run.seed})</code>. {run.guide}
              </li>
            ))}
          </ol>
          <p>
            Take the flows in any order. At every step each Show filter works, and so does the case view of the orders each
            filter lists first. The receipts open as the files the API served while recording.
          </p>
          <p>
            Creating a run with a recorded scenario and seed 101 opens its recorded run. Not in the recording: other
            scenarios or seeds, advancing the replay clock, the Local model and Recorded run modes, other corrected handlers
            and other step orders. Each answers with a message saying so. Opening a run clears the page's
            diagnosis view, as it does with the real server, so a flow left half-way needs Restart the demo to finish.
          </p>
        </>
      )}
    </details>
  );
}
