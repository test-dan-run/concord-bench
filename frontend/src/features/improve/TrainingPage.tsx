// Phase 3 (Improve) entry point. Only in concord-full; the lite build never includes this chunk.
// On hold until the owner confirms scope and how training jobs are launched (Q5).

import { useQuery } from "@tanstack/react-query";
import { api } from "../../lib/api";
import { PageHeader, Panel } from "../../components/ui";

export default function TrainingPage() {
  const { data } = useQuery({ queryKey: ["improve-status"], queryFn: () => api.get<{ state: string; reason: string }>("/improve/status") });
  return (
    <>
      <PageHeader crumbs="Improve" title="Training" subtitle="Retraining and fine-tuning from confirmed labels." />
      <Panel title="Not built yet">
        <span className="text-[13px] text-muted">Phase 3 is {data?.state ?? "on hold"}. {data?.reason}</span>
      </Panel>
    </>
  );
}
