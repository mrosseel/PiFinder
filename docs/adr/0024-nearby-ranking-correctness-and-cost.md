# Nearby ranking: haversine takes (Dec, RA), rank a bounded window, and don't drag the cursor while slewing

The object list's "Nearby" sort (`PiFinder/nearby.py`, driven from
`UIObjectList`) has two user-visible faults: the focused row is often not the
closest object, and the list stalls the UI while the scope slews. They have
three independent causes — a wrong metric, an unbounded ranking, and a cursor
policy borrowed from a different situation. This ADR records the decisions for
all three, because fixing any one alone leaves the symptom.

## 1. The BallTree is fed (RA, Dec); haversine means (lat, lon)

`ClosestObjectsFinder.calculate_objects_balltree` builds rows as
`[deg2rad(ra), deg2rad(dec)]` and `get_closest_objects` /
`get_objects_within_radius` query with the same ordering, against
`metric="haversine"`. scikit-learn's haversine metric reads dimension 0 as
**latitude** and dimension 1 as **longitude**. The code therefore hands it RA as
latitude and Dec as longitude, and every distance is computed on a swapped
sphere.

The separation is correct only when both points share a meridian (`ra1 == ra2`),
where the metric degenerates to `|dec1 - dec2|`. Everywhere else it is wrong,
and the error grows with declination.

Worked example — pointing at RA 0°, Dec +60°:

| object | RA | Dec | true separation | ranked distance today | rank today |
|--------|----|-----|-----------------|-----------------------|------------|
| A | 10° | +60° | **5.00°** | 10.00° | 2nd |
| B | 0°  | +50° | 10.00° | 10.00° | **1st** |
| C | 90° | +60° | 41.41° | 90.00° | 3rd |
| D | 180°| +62° | 58.00° | 178.00° | 4th |

The genuinely nearest object is not first. This is the whole of "the focused
thing is not the nearest thing" on the correctness side.

The same tree backs the chart's nearby-DSO marker layer
(`UIChart._get_nearby_markers` → `get_objects_within_radius`), so the chart
plots the wrong set of markers. Measured recall for a 5° radius query over a
uniform 20 000-object sky:

| pointing Dec | objects truly in radius | returned | correct | recall |
|--------------|------------------------|----------|---------|--------|
| 0°  | 36 | 59 | 36 | 100% (23 false) |
| 40° | 34 | 58 | 28 | 82% |
| 60° | 46 | 41 | 32 | 70% |
| 85° | 41 | 12 |  7 | **17%** |

Near the equator the query over-includes but never misses, which is why this
survived: the failure is invisible exactly where casual testing happens.

**Decision:** build the tree and query it as `[dec_rad, ra_rad]`. This is a
one-line change in each of the three places, and the radius unit
(`deg2rad(radius_deg)`) is already correct.

**Decision:** `tests/test_nearby.py` places every object at `ra = 0` and its
docstring calls the `[ra, dec]` ordering "the pre-existing convention". That is
the one line on which the bug cannot show. The tests must place objects off a
shared meridian and assert against an independently computed great-circle
separation, so the axis order is pinned rather than assumed. Add a
high-declination case (Dec ≥ 60°), which is where the swap bites hardest.

## 2. Ranking the whole catalog to draw nine rows

`get_closest_objects(ra, dec)` is always called with the default `n=0`, which it
expands to `n = len(self._objects)`. Every refresh therefore asks the BallTree
for a full k = N ordering and materialises an N-element object array
(`self._objects[obj_ind[0]]`), so that `UIObjectList` can draw about nine rows.

Cost, measured on a fast x86-class dev machine (a Pi is roughly an order of
magnitude slower):

| N | k = N query | k = 200 query |
|---|-------------|---------------|
| 14 000 |  1.5 ms | 0.09 ms |
| 40 000 |  4.4 ms | 0.13 ms |

Replacing the BallTree with a vectorised haversine plus `argsort` was measured
at 0.9 ms / 4.0 ms — no real gain. The cost is inherent to producing a **total**
ordering; it cannot be optimised away, only avoided.

The larger cost sits one level up. Each refresh also runs `_next_target_index`,
which builds a `(catalog_code, sequence)` dict over the entire new ordering in
pure Python: **7.6 ms at N = 14 000, 22 ms at N = 40 000** on the same machine.
On a Pi this alone can exceed the 33 ms frame budget.

**Decision:** the Nearby sort produces a bounded window, not a total ordering.
Introduce `NEAREST_LIST_CAP` (start at 200) and query `k = min(cap, N)`.

The trade-off, stated plainly: you can no longer scroll a Nearby-sorted list
down to the object on the far side of the sky. That ordering has no observing
use — an object 140° away is not "nearby" under any reading — and paying an
O(N) cost on every degree of slew to keep it available is the wrong bargain. If
a user reaches the end of the window, the list simply ends; switching to Catalog
or RA sort still exposes everything.

## 3. The refresh trigger is per-axis degrees, not angular distance

`Nearby.should_refresh` compares `abs(ra - last_ra) > MAX_DEVIATION` with
`MAX_DEVIATION = 1.0`. RA degrees are not sky degrees:

* One degree of RA spans `cos(dec)` degrees on the sky — 0.17° at Dec 80°,
  0.017° at Dec 89°. Near the pole the list re-ranks for movement the user
  cannot see, which is exactly where slewing is slowest and the stall most
  noticeable.
* RA wraps. Crossing RA 0 gives `abs(359.5 - 0.5) = 359`, so the trigger is
  permanently true in a band around the meridian. It never *misses* a refresh,
  so it is not a correctness bug — it is a cost bug, and an invisible one.

`MAX_TIME = 2` additionally forces a full re-rank every two seconds on a
perfectly stationary scope, where by construction nothing has changed.

**Decision:** trigger on great-circle separation from `(last_ra, last_dec)`,
using the same haversine the ranking uses, against a `MAX_DEVIATION` now read as
true sky degrees. **Decision:** raise the time trigger to
`MAX_TIME = 10` s — it exists to pick up catalog/filter changes and altitude
drift, not pointing changes, and 2 s buys nothing at 15°/hour of sky rotation.

## 4. The cursor should not follow the old object while slewing

`UIObjectList.update` calls `_next_target_index` after every Nearby refresh, to
hold the cursor on the previously selected object as the list is rebuilt. For a
**filter-driven** rebuild that is right, and it is why the helper exists (see
ADR 0020): the user logged an object or tightened a filter, and expects to land
on the natural next target.

For a **pointing-driven** re-rank it inverts the user's intent. The user slews
the scope *in order to change what is nearest*. Dragging the cursor along with
the object they were previously on means the focused row drifts down the list
and away from the object they just pointed at. Only `sort()` resets to index 0,
and that runs only when the sort order is (re)selected from the marking menu.
This is the second half of "the focused thing is not the nearest thing", and it
is a policy fault, not a bug — the code does exactly what it says.

**Decision:** in `SortOrder.NEAREST`, distinguish the two rebuild causes.

* A pointing-driven refresh **holds the cursor at index 0** while the user has
  not scrolled. The top row stays the nearest object, which is what the mode is
  for.
* Once the user scrolls off the top, the list is being browsed, and the cursor
  pins to the selected object via `_next_target_index` exactly as today.
  Scrolling back to the top re-arms the follow-the-nearest behaviour.
* A filter-driven rebuild (`refresh_object_list`) keeps today's behaviour
  unconditionally.

No new state is needed: "the user has not scrolled" is exactly
`_current_item_index == 0`, which the re-rank reads before it replaces the
list. No new mode, and no user-facing setting.

## 5. Smaller faults fixed alongside

These are cheap, sit in the same call path, and would otherwise be re-discovered
by the next reader:

* **Redundant full query.** `mm_change_sort` calls `nearby_refresh()` *before*
  `sort()`. At that moment `set_items()` has not run for the current filtered
  set, so it ranks against a stale or empty tree; `sort()` then immediately
  redoes the work correctly. Drop the first call.
* **Unreachable message.** `Nearby.refresh()` returns `[]` when there is no
  pointing, never `None`, but `nearby_refresh()` tests `is None` before showing
  "No Solve Yet". The message cannot fire; the user sees "No objects match
  filter" instead. Test for emptiness with the no-pointing case distinguished.
* **`SortOrder.RA` is not implemented.** `sort()` has branches for `NEAREST` and
  `CATALOG_SEQUENCE` only, so choosing RA leaves the list in whatever order it
  already had, and the two-way label in `update()` renders it as "Nearby".
  Shipping a sort mode that silently does nothing is worse than not offering
  it, and the ordering is one `sorted(key=…ra)` call — implement it, and route
  both labels through a single `_sort_order_label` helper so a fourth order
  cannot reintroduce the mismatch.
* **Uncached tree rebuild.** `sort()` calls `set_items()` →
  `calculate_objects_balltree` every time, while `UIChart` already guards the
  identical rebuild on `catalog_filter.dirty_time`. The object list adopts the
  same guard, invalidated explicitly by `refresh_object_list` — which rebuilds
  `_menu_items` from source, and so invalidates the index whatever the
  filter's `dirty_time` says.
* **Type hint.** `get_closest_objects` is annotated `List[CompositeObject]` but
  returns a NumPy object array. Callers only iterate and index, so nothing
  breaks today; make the annotation honest.

## Consequences

* Nearby ranking becomes correct at all declinations, and the chart's nearby
  marker layer with it — a fix worth more than the list ordering, since a
  missing marker is silent.
* Per-refresh cost drops from O(N) to O(cap) in both the query and the
  cursor-tracking helper, which removes the re-rank from the frame budget and
  with it the slewing stall.
* Nearby lists are truncated to `NEAREST_LIST_CAP`. This is the one behaviour
  users may notice as a loss.
* Objects that were ranked "near" only because of the swapped metric will move
  or disappear from the list. Anyone who learned the old ordering will see it
  change, which is the point.
