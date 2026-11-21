import { describe, it, expect } from "vitest";
import { render, screen } from "@testing-library/react";

const DummyComponent = () => <div>Hello Antigravity Component Test</div>;

describe("Component Test Setup", () => {
  it("proves Vitest, jsdom, and Testing Library render and query a component", () => {
    render(<DummyComponent />);
    expect(screen.getByText("Hello Antigravity Component Test")).toBeDefined();
  });
});
