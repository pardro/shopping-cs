from app.models import ActionType, ChannelName, ExecutionPlan, PlannedAction


class InformationCollectorAgent:
    def add_freshness_steps(self, plan: ExecutionPlan) -> ExecutionPlan:
        optimized_actions: list[PlannedAction] = []
        synced_channels: set[ChannelName] = set()
        synced_all = False

        for action in plan.actions:
            if action.type == ActionType.SYNC:
                optimized_actions.append(action)
                if action.channel:
                    synced_channels.add(action.channel)
                else:
                    synced_all = True
                continue

            sync_action = self._sync_action_needed(action, synced_channels, synced_all)
            if sync_action:
                optimized_actions.append(sync_action)
                if sync_action.channel:
                    synced_channels.add(sync_action.channel)
                else:
                    synced_all = True

            optimized_actions.append(action)

        if optimized_actions == plan.actions:
            return plan
        return plan.model_copy(update={"actions": optimized_actions})

    @staticmethod
    def split_information_actions(plan: ExecutionPlan) -> tuple[ExecutionPlan, ExecutionPlan]:
        information_actions = [
            action for action in plan.actions if action.type == ActionType.SYNC
        ]
        execution_actions = [
            action for action in plan.actions if action.type != ActionType.SYNC
        ]
        return (
            plan.model_copy(update={"actions": information_actions}),
            plan.model_copy(update={"actions": execution_actions}),
        )

    @staticmethod
    def _sync_action_needed(
        action: PlannedAction,
        synced_channels: set[ChannelName],
        synced_all: bool,
    ) -> PlannedAction | None:
        if action.type == ActionType.SUMMARY:
            if synced_all:
                return None
            if action.channel:
                if action.channel in synced_channels:
                    return None
                return PlannedAction(
                    type=ActionType.SYNC,
                    channel=action.channel,
                    reason="최신 문의 목록을 기준으로 해당 채널을 보여주기 위해 먼저 동기화합니다.",
                    prepared_api=f"GET {action.channel.value} 대화 목록 API",
                )
            return PlannedAction(
                type=ActionType.SYNC,
                reason="최신 문의 목록을 기준으로 요약하기 위해 먼저 모든 채널을 동기화합니다.",
                prepared_api="GET kakao/naver 대화 목록 API",
            )

        if action.type in {
            ActionType.CONVERSATION_DETAIL,
            ActionType.ORDER_LOOKUP,
            ActionType.DRAFT_REPLY,
            ActionType.SEND_REPLY,
        }:
            if synced_all:
                return None
            if action.channel:
                if action.channel in synced_channels:
                    return None
                return PlannedAction(
                    type=ActionType.SYNC,
                    channel=action.channel,
                    reason="최신 전체 대화 기록을 기준으로 처리하기 위해 해당 채널을 먼저 동기화합니다.",
                    prepared_api=f"GET {action.channel.value} 대화 목록 API",
                )
            return PlannedAction(
                type=ActionType.SYNC,
                reason="대상 문의를 판단하기 전에 최신 전체 문의 목록을 수집합니다.",
                prepared_api="GET kakao/naver 대화 목록 API",
            )

        return None
