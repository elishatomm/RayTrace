import { replayTime } from "../format";
import type { EvidencePacket } from "../types";
import { JsonText, UntrustedText } from "./JsonText";

interface EvidencePanelProps {
  packet: EvidencePacket | null;
  selectedId: string | null;
  onSelect: (evidenceId: string | null) => void;
}

export function EvidencePanel({ packet, selectedId, onSelect }: Readonly<EvidencePanelProps>) {
  if (packet === null) {
    return (
      <section className="panel evidence" id="evidence">
        <h2>Evidence</h2>
        <p className="note">Run a diagnosis to assemble the evidence packet for the incident.</p>
      </section>
    );
  }
  const card = packet.incident;
  return (
    <section className="panel evidence" id="evidence">
      <h2>Evidence packet {packet.packet_version}</h2>
      <p className="lead">
        {card.symptom}. Trigger <code>{card.trigger}</code>; window {replayTime(card.window_start_ms)} to {replayTime(card.window_end_ms)};
        evidence cutoff {replayTime(card.evidence_cutoff_ms)}.
      </p>
      <p className="note">
        Candidate deployments: {packet.candidate_change_ids.join(", ") || "none"}. Candidate methods:{" "}
        {packet.candidate_methods.join(", ") || "none"}.
      </p>
      <ul className="evidence-list">
        {packet.items.map((item) => (
          <li key={item.evidence_id} id={`evidence-${item.evidence_id}`} className={item.evidence_id === selectedId ? "selected" : ""}>
            <button
              type="button"
              className="link evidence-id"
              aria-expanded={item.evidence_id === selectedId}
              onClick={() => onSelect(item.evidence_id === selectedId ? null : item.evidence_id)}
            >
              [{item.evidence_id}]
            </button>{" "}
            <span className="evidence-kind">{item.kind}</span>, known {replayTime(item.known_at_ms)}: {item.title}
            {item.evidence_id === selectedId && (
              <div className="evidence-detail">
                <JsonText value={item.facts} />
                {item.text !== null && <UntrustedText text={item.text} />}
              </div>
            )}
          </li>
        ))}
      </ul>
    </section>
  );
}
