import { cleanup, fireEvent, render } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";
import type { OfflineAppChoice } from "../../lib/api";
import { AppChoice } from "./ExportOffline";

const label: OfflineAppChoice = {
  app: "concord-label", title: "Concord Label", port: 8620, own: false, default_image: "concord-label:0.2.0",
  images: [{ image: "concord-label:0.2.0", present: true, bytes: 1.8e9 }],
};

afterEach(cleanup);

describe("AppChoice", () => {
  it("shows the local image tag of the other app and ticks it", () => {
    const seen: { on: boolean; image: string | null }[] = [];
    const { getByLabelText, container } = render(<AppChoice choice={label} on={false} image="concord-label:0.2.0" onChange={(n) => seen.push(n)} />);
    expect(container.textContent).toContain("concord-label:0.2.0");
    fireEvent.click(getByLabelText(/Include Concord Label/));
    expect(seen).toEqual([{ on: true, image: "concord-label:0.2.0" }]);
  });
  it("offers a choice when several tags are present", () => {
    const two = { ...label, images: [...label.images, { image: "concord-label:0.1.0", present: true, bytes: 1.7e9 }] };
    const { getByLabelText } = render(<AppChoice choice={two} on image="concord-label:0.2.0" onChange={() => {}} />);
    expect((getByLabelText("Concord Label image") as HTMLSelectElement).options.length).toBe(2);
  });
  it("says how to build a missing image and cannot be ticked", () => {
    const none = { ...label, images: [], default_image: null };
    const { getByLabelText, container } = render(<AppChoice choice={none} on={false} image={null} onChange={() => {}} />);
    expect(container.textContent).toContain("image not found — run just build in that repo");
    expect((getByLabelText(/Include Concord Label/) as HTMLInputElement).disabled).toBe(true);
  });
});
