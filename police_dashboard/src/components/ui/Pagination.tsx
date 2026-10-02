import { ChevronLeft, ChevronRight } from "lucide-react";
import { Button } from "./Button";

/**
 * Client-side pagination for tables whose API has no limit/offset parameter.
 * The backend endpoints used by this console return the full list, so the
 * page slice is computed here rather than changing any request shape.
 */
export function Pagination({
  page,
  pageCount,
  total,
  pageSize,
  onPage,
}: {
  page: number;
  pageCount: number;
  total: number;
  pageSize: number;
  onPage: (page: number) => void;
}) {
  if (pageCount <= 1) {
    return (
      <p className="px-4 py-3 text-xs text-surface-muted">
        {total} row{total === 1 ? "" : "s"}
      </p>
    );
  }

  const first = page * pageSize + 1;
  const last = Math.min(total, (page + 1) * pageSize);

  return (
    <div className="flex flex-wrap items-center justify-between gap-3 border-t border-surface-border px-4 py-3">
      <p className="text-xs text-surface-muted">
        Showing {first}–{last} of {total}
      </p>
      <div className="flex items-center gap-2">
        <Button
          variant="secondary"
          size="sm"
          disabled={page === 0}
          onClick={() => onPage(page - 1)}
          icon={<ChevronLeft className="h-4 w-4" aria-hidden />}
        >
          Prev
        </Button>
        <span className="px-1 text-xs font-medium text-surface-muted">
          {page + 1} / {pageCount}
        </span>
        <Button
          variant="secondary"
          size="sm"
          disabled={page >= pageCount - 1}
          onClick={() => onPage(page + 1)}
        >
          Next
          <ChevronRight className="h-4 w-4" aria-hidden />
        </Button>
      </div>
    </div>
  );
}

export default Pagination;
