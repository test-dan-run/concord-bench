import { createBrowserRouter, Navigate, Outlet, useRouteError } from "react-router-dom";
import { Nav } from "./Nav";
import { BenchmarksPage } from "../features/benchmarks/BenchmarksPage";
import { AsrErrors } from "../features/errors/AsrErrors";
import { DiarErrors } from "../features/errors/DiarErrors";
import { ErrorsIndex } from "../features/errors/ErrorsIndex";
import { PipelineErrors } from "../features/errors/PipelineErrors";
import { DatasetDetail } from "../features/datasets/DatasetDetail";
import { DatasetsPage } from "../features/datasets/DatasetsPage";
import { UploadDataset } from "../features/datasets/UploadDataset";
import { ImportReference } from "../features/datasets/ImportReference";
import { ModelsPage } from "../features/models/ModelsPage";
import { JobsPage } from "../features/jobs/JobsPage";

function Layout() {
  return (
    <div className="min-h-screen bg-canvas text-fg flex flex-col lg:flex-row">
      <Nav />
      <main className="flex-1 min-w-0 px-4 py-6 sm:px-8 sm:pt-7 sm:pb-10 flex flex-col gap-5">
        <Outlet />
      </main>
    </div>
  );
}

function RouteError() {
  const err = useRouteError() as Error;
  return (
    <div className="p-8 text-fg">
      <h1 className="text-lg">Something went wrong</h1>
      <pre className="text-muted text-xs whitespace-pre-wrap">{err?.message ?? String(err)}</pre>
    </div>
  );
}

export const router = createBrowserRouter([
  {
    path: "/",
    element: <Layout />,
    errorElement: <RouteError />,
    children: [
      { index: true, element: <Navigate to="/benchmarks" replace /> },
      { path: "benchmarks", element: <BenchmarksPage /> },
      { path: "benchmarks/:tab", element: <BenchmarksPage /> },
      { path: "errors", element: <ErrorsIndex /> },
      { path: "errors/asr/:runId", element: <AsrErrors /> },
      { path: "errors/diar/:runId", element: <DiarErrors /> },
      { path: "errors/pipeline/:runId", element: <PipelineErrors /> },
      { path: "datasets", element: <DatasetsPage /> },
      { path: "datasets/upload", element: <UploadDataset /> },
      { path: "datasets/import", element: <ImportReference /> },
      { path: "datasets/:id", element: <DatasetDetail /> },
      { path: "models", element: <ModelsPage /> },
      { path: "jobs", element: <JobsPage /> },
      { path: "*", element: <Navigate to="/benchmarks" replace /> },
    ],
  },
]);
