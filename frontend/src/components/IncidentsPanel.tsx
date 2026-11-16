import { duration, ratio, replayTime, rupees } from "../format";
import type { IncidentsResponse, ProviderMeasure } from "../types";

function cohortName(measure: ProviderMeasure): string {
  return measure.cohort === "all" ? "all methods" : measure.cohort;
}

// The metric definitions the API sends, in the order the card shows their numbers. Each one is optional text.
function Definitions({ definitions }: Readonly<{ definitions: Record<string, string> }>) {
  const entries: [string, string | undefined][] = [
    ["Provider authorisation", definitions.provider_authorisation],
    ["Freshness", definitions.provider_freshness],
    ["Merchant completion", definitions.merchant_completion],
    ["Pending captured value", definitions.pending_captured_value],
  ];
  return (
    <details className="definitions">
      <summary>How these numbers are defined</summary>
      <dl>
        {entries.filter(([, text]) => text !== undefined).map(([term, text]) => (
          <div key={term}>
            <dt>{term}</dt>
            <dd>{text}</dd>
          </div>
        ))}
      </dl>
    </details>
  );
}

export function IncidentsPanel({ observed }: Readonly<{ observed: IncidentsResponse }>) {
  const merchant = observed.merchant;
  return (
    <>
      <section className="panel incidents">
        <h2>Incidents</h2>
        {observed.incidents.length === 0 && <p className="note">No detector has opened an incident by this replay time.</p>}
        <ul className="incident-list">
          {observed.incidents.map((incident) => (
            <li key={incident.incident_id}>
              <strong className="mono">{incident.incident_id}</strong> <span className="chip tone-stuck">{incident.state}</span>: trigger{" "}
              <code>{incident.trigger}</code>, cohort <code>{JSON.stringify(incident.cohort)}</code>, opened with evidence up to{" "}
              {replayTime(incident.evidence_cutoff_ms)}
              {incident.fired_detectors && incident.fired_detectors.length > 0 && (
                <> (fired: {incident.fired_detectors.join(", ")})</>
              )}
            </li>
          ))}
        </ul>
        <h3>Detectors now</h3>
        <ul className="compact detectors">
          {observed.detectors_now.map((result, index) => (
            <li key={`${result.detector}-${index}`} className={result.fired ? "fired" : "quiet"}>
              <code>{result.detector}</code> <code>{JSON.stringify(result.cohort)}</code>:{" "}
              <strong className={result.fired ? "tone-text tone-stuck" : "tone-text tone-neutral"}>{result.fired ? "FIRED" : "quiet"}</strong>{" "}
              ({result.reason})
            </li>
          ))}
        </ul>
      </section>

      <section className="panel metrics">
        <h2>Provider versus merchant, as of {replayTime(observed.run.as_of_ms)}</h2>
        <h3>Provider authorisation (current mature cohort)</h3>
        <div className="table-scroll">
          <table>
            <thead>
              <tr>
                <th>Cohort</th><th className="num">Authorised</th><th className="num">Captured</th>
                <th className="num">Unresolved</th><th className="num">Baseline authorised</th><th className="num">Freshness</th>
              </tr>
            </thead>
            <tbody>
              {observed.provider.current.map((measure) => {
                const baseline = observed.provider.baseline.find((item) => item.cohort === measure.cohort);
                return (
                  <tr key={measure.cohort}>
                    <td>{cohortName(measure)}</td>
                    <td className="num">{ratio(measure.authorization)}</td>
                    <td className="num">{ratio(measure.capture)}</td>
                    <td className="num">{ratio(measure.unresolved)}</td>
                    <td className="num">{baseline === undefined ? "n/a" : ratio(baseline.authorization)}</td>
                    <td className="num">{duration(measure.freshness_ms)}</td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
        <h3>Merchant processing</h3>
        <p className="metric">Completion: <span className="figure-inline">{ratio(merchant.completion)}</span></p>
        <p className="metric">
          Pending captured value (metric): <span className="figure-inline">{rupees(merchant.pending_captured_value_paise)}</span> over{" "}
          {merchant.pending_captured_orders} orders
        </p>
        <p className="metric">
          Inbox backlog: {Object.entries(observed.backlog).map(([status, count]) => `${status} ${count}`).join(", ")};
          oldest unprocessed {duration(merchant.oldest_backlog_age_ms)}; worker failures {merchant.worker_failures}
        </p>
        <h3>Deployments known by now</h3>
        {observed.deployments.length === 0 ? (
          <p className="note">None in this run.</p>
        ) : (
          <ul className="compact deployments">
            {observed.deployments.map((item) => (
              <li key={item.deploy_id}>
                <code>{item.deploy_id}</code> to {item.service_id} ({item.kind}) at {replayTime(item.deployed_at_ms)}: {item.summary}
              </li>
            ))}
          </ul>
        )}
        <Definitions definitions={observed.definitions} />
      </section>
    </>
  );
}
