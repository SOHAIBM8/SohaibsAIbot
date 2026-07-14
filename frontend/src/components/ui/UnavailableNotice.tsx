/**
 * Renders the honest "no data source yet" stub the backend returns
 * for positions/equity-curve/mode/today-pnl (available=false + reason)
 * — always distinct from an empty-but-real list, per those schemas'
 * own docstrings (api/schemas/*.py): an empty list and "we cannot
 * tell you" are different facts and must never render identically.
 */
export function UnavailableNotice({ reason }: { reason: string }) {
  return (
    <div className="rounded border border-dashed border-gray-300 bg-gray-50 p-3 text-sm text-gray-500 dark:border-gray-700 dark:bg-gray-900 dark:text-gray-400">
      <span className="font-medium">Not yet available.</span> {reason}
    </div>
  );
}
