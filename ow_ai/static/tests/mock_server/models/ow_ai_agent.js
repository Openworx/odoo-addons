import { mailDataHelpers } from "@mail/../tests/mock_server/mail_mock_server";

import { Command, fields, getKwArgs, models } from "@web/../tests/web_test_helpers";

/** Mock of `ow.ai.agent`: only what the chat client reads. */
export class OwAiAgent extends models.ServerModel {
    _name = "ow.ai.agent";

    name = fields.Char();
    subtitle = fields.Char();
    partner_id = fields.Many2one({ relation: "res.partner" });

    /**
     * Mirrors `ow.ai.agent._create_chat_channel`: an `ow_ai_chat` channel with
     * the user and the agent partner as its only members, plus its root session.
     *
     * @param {number} agentId
     * @param {number} userPartnerId
     * @param {{ title?: string, resModel?: string, resId?: number, composerId?: number }} [options]
     * @returns {{ channelId: number, sessionId: number }}
     */
    _create_chat_channel(agentId, userPartnerId, { title, resModel, resId, composerId } = {}) {
        const [agent] = this.browse(agentId);
        const channelId = this.env["discuss.channel"].create({
            channel_type: "ow_ai_chat",
            channel_member_ids: [
                Command.create({ partner_id: userPartnerId }),
                Command.create({ partner_id: agent.partner_id }),
            ],
            // no title: nameless, to exercise the client's fallback to the agent's name
            name: title || false,
            ow_ai_agent_id: agent.id,
        });
        const sessionId = this.env["ow.ai.session"].create({
            agent_id: agent.id,
            channel_id: channelId,
            composer_id: composerId || false,
            res_model: resModel || false,
            res_id: resId || false,
            // `ow.ai.agent._create_session`: on when web search is configured (default setting)
            web_search: false, // `ow_ai.web_search_default` is off: new chats start with the switch off
        });
        return { channelId, sessionId };
    }

    /**
     * Mirrors `ow.ai.agent.action_launch_chat`: a fresh chat for the current
     * user, its Store payload, the composer's prompt buttons and a subtitle.
     * The agent is the composer's (`interface_key`), else the first one.
     */
    action_launch_chat(ids, interface_key, res_model, res_id, channel_title) {
        const kwargs = getKwArgs(
            arguments,
            "ids",
            "interface_key",
            "res_model",
            "res_id",
            "channel_title"
        );
        const [composer] = this.env["ow.ai.composer"]._filter([
            ["interface_key", "=", kwargs.interface_key || "systray"],
        ]);
        const agentId = kwargs.ids?.[0] || composer?.agent_id || this.search([])[0];
        const [agent] = this.browse(agentId);
        let title = kwargs.channel_title;
        if (kwargs.res_model && kwargs.res_id) {
            title ||= this.env[kwargs.res_model].browse(kwargs.res_id)[0]?.display_name;
        }
        const { channelId, sessionId } = this._create_chat_channel(
            agent.id,
            this.env.user.partner_id,
            {
                title: title || agent.name,
                resModel: kwargs.res_model,
                resId: kwargs.res_id,
                composerId: composer?.id,
            }
        );
        const store = new mailDataHelpers.Store(this.env["discuss.channel"].browse(channelId));
        return {
            channel_id: channelId,
            session_id: sessionId,
            store_data: store.get_result(),
            prompt_buttons: composer?.prompt_buttons || [],
            subtitle: agent.subtitle || false,
        };
    }
}
