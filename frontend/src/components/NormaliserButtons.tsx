// The text normaliser as segmented buttons, shared by Benchmarks and every error page (lib/config holds
// the choice, so it stays the same everywhere).

import { useNormaliser, useNormalisers } from "../lib/config";
import { Segmented } from "./ui";

export function NormaliserButtons() {
  const { data: norms } = useNormalisers();
  const [normaliser, setNormaliser] = useNormaliser();
  return (
    <Segmented
      label="Text normaliser"
      value={normaliser}
      onChange={setNormaliser}
      options={(norms ?? []).map((n) => ({ value: n.id, label: n.label, title: n.description }))}
    />
  );
}
