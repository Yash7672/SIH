import { createContext, useContext, type ReactNode } from "react";
import type { PoliceSocketState } from "./usePoliceSocket";

/**
 * Shares the single alert-socket state with pages that need to report on it.
 *
 * Calling usePoliceSocket() from a page as well would open a *second* socket to
 * the same endpoint, and every detection would then be delivered - and rendered
 * - twice. The socket is owned by the layout, exactly once, and published here.
 */
const SocketStateContext = createContext<PoliceSocketState | null>(null);

export function SocketStateProvider({
  value,
  children,
}: {
  value: PoliceSocketState;
  children: ReactNode;
}) {
  return <SocketStateContext.Provider value={value}>{children}</SocketStateContext.Provider>;
}

export function useSocketState(): PoliceSocketState {
  const state = useContext(SocketStateContext);
  // Failing loudly beats rendering a page that silently claims the feed is fine.
  if (!state) throw new Error("useSocketState must be used inside Layout (SocketStateProvider)");
  return state;
}