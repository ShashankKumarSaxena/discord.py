"""
The MIT License (MIT)

Copyright (c) 2015-present Rapptz

Permission is hereby granted, free of charge, to any person obtaining a
copy of this software and associated documentation files (the "Software"),
to deal in the Software without restriction, including without limitation
the rights to use, copy, modify, merge, publish, distribute, sublicense,
and/or sell copies of the Software, and to permit persons to whom the
Software is furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in
all copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS
OR IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING
FROM, OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER
DEALINGS IN THE SOFTWARE.
"""

from __future__ import annotations
from typing import Any, Callable, ClassVar, Dict, Iterator, List, Optional, TYPE_CHECKING, Tuple
from functools import partial
from itertools import groupby

import asyncio
import sys
import time
import os
from .item import Item, ItemCallbackType
from ..enums import ComponentType
from ..components import (
    Component,
    ActionRow as ActionRowComponent,
    _component_factory,
    Button as ButtonComponent,
)

__all__ = (
    'View',
)


if TYPE_CHECKING:
    from ..interactions import Interaction
    from ..types.components import Component as ComponentPayload
    from ..state import ConnectionState


def _walk_all_components(components: List[Component]) -> Iterator[Component]:
    for item in components:
        if isinstance(item, ActionRowComponent):
            yield from item.children
        else:
            yield item


def _component_to_item(component: Component) -> Item:
    if isinstance(component, ButtonComponent):
        from .button import Button

        return Button.from_component(component)
    return Item.from_component(component)


class View:
    """Represents a UI view.

    This object must be inherited to create a UI within Discord.

    Parameters
    -----------
    timeout: Optional[:class:`float`]
        Timeout from last interaction with the UI before no longer accepting input.
        If ``None`` then there is no timeout.

    Attributes
    ------------
    timeout: Optional[:class:`float`]
        Timeout from last interaction with the UI before no longer accepting input.
        If ``None`` then there is no timeout.
    children: List[:class:`Item`]
        The list of children attached to this view.
    """

    __slots__ = (
        'timeout',
        'children',
        'id',
        '_cancel_callback',
    )

    __discord_ui_view__: ClassVar[bool] = True
    __view_children_items__: ClassVar[List[ItemCallbackType]] = []

    def __init_subclass__(cls) -> None:
        children: List[ItemCallbackType] = []
        for base in reversed(cls.__mro__):
            for member in base.__dict__.values():
                if hasattr(member, '__discord_ui_model_type__'):
                    children.append(member)

        if len(children) > 25:
            raise TypeError('View cannot have more than 25 children')

        cls.__view_children_items__ = children

    def __init__(self, timeout: Optional[float] = 180.0):
        self.timeout = timeout
        self.children: List[Item] = []
        for func in self.__view_children_items__:
            item: Item = func.__discord_ui_model_type__(**func.__discord_ui_model_kwargs__)
            item.callback = partial(func, self, item)
            item._view = self
            setattr(self, func.__name__, item)
            self.children.append(item)

        loop = asyncio.get_running_loop()
        self.id = os.urandom(16).hex()
        self._cancel_callback: Optional[Callable[[View], None]] = None
        self._timeout_handler: Optional[asyncio.TimerHandle] = None
        self._stopped = loop.create_future()

    def to_components(self) -> List[Dict[str, Any]]:
        def key(item: Item) -> int:
            if item.group_id is None:
                return sys.maxsize
            return item.group_id

        children = sorted(self.children, key=key)
        components: List[Dict[str, Any]] = []
        for _, group in groupby(children, key=key):
            group = list(group)
            if len(group) <= 5:
                components.append(
                    {
                        'type': 1,
                        'components': [item.to_component_dict() for item in group],
                    }
                )
            else:
                components.extend(
                    {
                        'type': 1,
                        'components': [item.to_component_dict() for item in group[index : index + 5]],
                    }
                    for index in range(0, len(group), 5)
                )

        return components

    @property
    def _expires_at(self) -> Optional[float]:
        if self.timeout:
            return time.monotonic() + self.timeout
        return None

    def add_item(self, item: Item) -> None:
        """Adds an item to the view.

        Parameters
        -----------
        item: :class:`Item`
            The item to add to the view.

        Raises
        --------
        TypeError
            A :class:`Item` was not passed.
        ValueError
            Maximum number of children has been exceeded (25).
        """

        if len(self.children) > 25:
            raise ValueError('maximum number of children exceeded')

        if not isinstance(item, Item):
            raise TypeError(f'expected Item not {item.__class__!r}')

        item._view = self
        self.children.append(item)

    def remove_item(self, item: Item) -> None:
        """Removes an item from the view.

        Parameters
        -----------
        item: :class:`Item`
            The item to remove from the view.
        """

        try:
            self.children.remove(item)
        except ValueError:
            pass

    def clear_items(self) -> None:
        """Removes all items from the view."""
        self.children.clear()

    async def interaction_check(self, interaction: Interaction) -> bool:
        """|coro|

        A callback that is called when an interaction happens within the view
        that checks whether the view should process item callbacks for the interaction.

        This is useful to override if for example you want to ensure that the
        interaction author is a given user.

        The default implementation of this returns ``True``.

        .. note::

            If an exception occurs within the body then the interaction
            check is considered failed.

        Parameters
        -----------
        interaction: :class:`~discord.Interaction`
            The interaction that occurred.

        Returns
        ---------
        :class:`bool`
            Whether the view children's callbacks should be called.
        """
        return True

    async def on_timeout(self) -> None:
        """|coro|

        A callback that is called when a view's timeout elapses without being explicitly stopped.
        """
        pass

    async def _scheduled_task(self, state: Any, item: Item, interaction: Interaction):
        try:
            allow = await self.interaction_check(interaction)
        except Exception:
            allow = False

        if not allow:
            return

        await item.callback(interaction)
        if not interaction.response._responded:
            await interaction.response.defer()

    def _start_listening(self, store: ViewStore) -> None:
        self._cancel_callback = partial(store.remove_view)
        if self.timeout:
            loop = asyncio.get_running_loop()
            self._timeout_handler = loop.call_later(self.timeout, self.dispatch_timeout)

    def dispatch_timeout(self):
        self._stopped.set_result(True)
        asyncio.create_task(self.on_timeout(), name=f'discord-ui-view-timeout-{self.id}')

    def dispatch(self, state: Any, item: Item, interaction: Interaction):
        asyncio.create_task(self._scheduled_task(state, item, interaction), name=f'discord-ui-view-dispatch-{self.id}')

    def refresh(self, components: List[Component]):
        # This is pretty hacky at the moment
        # fmt: off
        old_state: Dict[Tuple[int, str], Item] = {
            (item.type.value, item.custom_id): item  # type: ignore
            for item in self.children
            if item.is_dispatchable()
        }
        # fmt: on
        children: List[Item] = []
        for component in _walk_all_components(components):
            try:
                older = old_state[(component.type.value, component.custom_id)]  # type: ignore
            except (KeyError, AttributeError):
                children.append(_component_to_item(component))
            else:
                older.refresh_component(component)
                children.append(older)

        self.children = children

    def stop(self) -> None:
        """Stops listening to interaction events from this view.

        This operation cannot be undone.
        """
        self._stopped.set_result(False)
        if self._timeout_handler:
            self._timeout_handler.cancel()

        if self._cancel_callback:
            self._cancel_callback(self)

    async def wait(self) -> bool:
        """Waits until the view has finished interacting.

        A view is considered finished when :meth:`stop` is called
        or it times out.

        Returns
        --------
        :class:`bool`
            If ``True``, then the view timed out. If ``False`` then
            the view finished normally.
        """
        return await self._stopped


class ViewStore:
    def __init__(self, state: ConnectionState):
        # (component_type, custom_id): (View, Item, Expiry)
        self._views: Dict[Tuple[int, str], Tuple[View, Item, Optional[float]]] = {}
        # message_id: View
        self._synced_message_views: Dict[int, View] = {}
        self._state: ConnectionState = state

    def __verify_integrity(self):
        to_remove: List[Tuple[int, str]] = []
        now = time.monotonic()
        for (k, (_, _, expiry)) in self._views.items():
            if expiry is not None and now >= expiry:
                to_remove.append(k)

        for k in to_remove:
            del self._views[k]

    def add_view(self, view: View, message_id: Optional[int] = None):
        self.__verify_integrity()

        expiry = view._expires_at
        view._start_listening(self)
        for item in view.children:
            if item.is_dispatchable():
                self._views[(item.type.value, item.custom_id)] = (view, item, expiry)  # type: ignore

        if message_id is not None:
            self._synced_message_views[message_id] = view

    def remove_view(self, view: View):
        for item in view.children:
            if item.is_dispatchable():
                self._views.pop((item.type.value, item.custom_id))  # type: ignore

        for key, value in self._synced_message_views.items():
            if value.id == view.id:
                del self._synced_message_views[key]
                break

    def dispatch(self, component_type: int, custom_id: str, interaction: Interaction):
        self.__verify_integrity()
        key = (component_type, custom_id)
        value = self._views.get(key)
        if value is None:
            return

        view, item, _ = value
        self._views[key] = (view, item, view._expires_at)
        item.refresh_state(interaction)
        view.dispatch(self._state, item, interaction)

    def is_message_tracked(self, message_id: int):
        return message_id in self._synced_message_views

    def update_from_message(self, message_id: int, components: List[ComponentPayload]):
        # pre-req: is_message_tracked == true
        view = self._synced_message_views[message_id]
        view.refresh([_component_factory(d) for d in components])
