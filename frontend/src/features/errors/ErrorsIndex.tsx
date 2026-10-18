import { Navigate } from "react-router-dom";
import { Empty, PageHeader, Spinner } from "../../components/ui";
import { useDoneRuns } from "./common";

/** "Error analysis" in the nav: open the most recent ASR run, else diarisation, else pipeline. */
export function ErrorsIndex() {
  const asr = useDoneRuns("asr");
  const seg = useDoneRuns("seg");
  const pipe = useDoneRuns("pipeline");
  if (asr.isLoading || seg.isLoading || pipe.isLoading) return <Spinner />;
  if (asr.data?.length) return <Navigate to={`/errors/asr/${asr.data[0].id}`} replace />;
  if (seg.data?.length) return <Navigate to={`/errors/diar/${seg.data[0].id}`} replace />;
  if (pipe.data?.length) return <Navigate to={`/errors/pipeline/${pipe.data[0].id}`} replace />;
  return (
    <>
      <PageHeader crumbs="Evaluate" title="Error analysis" />
      <Empty>No finished runs yet. Run a benchmark first; its errors show up here.</Empty>
    </>
  );
}
