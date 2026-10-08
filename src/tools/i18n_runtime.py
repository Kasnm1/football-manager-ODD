"""Small, dependency-free runtime helpers for stable API localization.

The API keeps its legacy ``error`` field while clients migrate to the
semantic ``message_key`` and ``message_params`` contract.  Rendered messages
always come from this registry; diagnostic error text is never interpolated
into a user-facing template.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping


DEFAULT_LOCALE = "en-GB"
SUPPORTED_LOCALES = (
    "en-GB", "zh-CN", "zh-TW", "ko-KR", "de-DE", "es-ES", "fr-FR", "ru-RU", "ja-JP",
    "pt-BR", "pt-PT",
)


MESSAGE_CATALOGS: dict[str, dict[str, str]] = {
    "en-GB": {
        "error.invalid_request": "The request is invalid.",
        "error.not_found": "The requested resource was not found.",
        "error.service_unavailable": "The local service is temporarily unavailable. Please try again.",
        "error.internal_error": "The server could not complete the request.",
        "error.numeric_overflow": "The supplied value is outside the supported range.",
        "error.request_failed": "The request could not be completed.",
        "error.resource_not_found": "{resource} was not found.",
        "integrity.waiver.mail_not_found": "This disciplinary penalty is no longer available. Refresh your mail and try again.",
        "integrity.waiver.unavailable": "This penalty can no longer be waived by payment.",
        "integrity.waiver.not_delivered": "This disciplinary notice has not been delivered yet.",
        "integrity.waiver.expired": "The payment deadline for this penalty has passed.",
        "integrity.waiver.insufficient_funds": "Your bank and betting wallet do not have enough funds to pay {amount}.",
        "referee_debt.mail_not_found": "This referee payment request is no longer available. Refresh your mail and try again.",
        "referee_debt.payment_unavailable": "This referee payment can no longer be completed because the penalty has already been enforced.",
        "referee_debt.payment_expired": "The deadline for this referee payment has passed.",
        "referee_debt.insufficient_funds": "Your bank and betting wallet do not have enough funds to pay {amount}.",
        "storage.migration.invalid_target": "The selected folder contains conflicting FMODD data or overlaps the current data folder. Choose another folder.",
        "storage.migration.failed": "FMODD could not copy your saved data. The original data is unchanged. Choose another folder or try again.",
        "storage.migration.busy": "FMODD is refreshing data. Wait for it to finish, then change the storage location.",
        "training.preferred_move.already_known": "The player already has this preferred move.",
        "training.preferred_move.not_known": "The player does not have this preferred move.",
    },
    "zh-CN": {
        "error.invalid_request": "请求无效。",
        "error.not_found": "未找到请求的资源。",
        "error.service_unavailable": "本地服务暂时不可用，请稍后重试。",
        "error.internal_error": "服务器无法完成该请求。",
        "error.numeric_overflow": "提供的数值超出了支持范围。",
        "error.request_failed": "无法完成该请求。",
        "error.resource_not_found": "未找到{resource}。",
        "integrity.waiver.mail_not_found": "该纪律处罚已不存在，请刷新邮件后重试。",
        "integrity.waiver.unavailable": "该处罚已不能通过缴款免除。",
        "integrity.waiver.not_delivered": "该纪律处罚通知尚未送达。",
        "integrity.waiver.expired": "该处罚的缴款期限已过。",
        "integrity.waiver.insufficient_funds": "银行与投注钱包余额不足，无法缴纳 {amount}。",
        "referee_debt.mail_not_found": "该裁判追债邮件已不存在，请刷新邮件后重试。",
        "referee_debt.payment_unavailable": "裁判追债处罚已经执行，无法再付款。",
        "referee_debt.payment_expired": "裁判追债的付款期限已过。",
        "referee_debt.insufficient_funds": "银行与投注钱包余额不足，无法支付 {amount}。",
        "storage.migration.invalid_target": "所选文件夹包含冲突的 FMODD 数据，或与当前数据目录重叠。请选择其他文件夹。",
        "storage.migration.failed": "FMODD 无法复制存档数据，原数据未被改动。请选择其他文件夹或重试。",
        "storage.migration.busy": "FMODD 正在刷新数据。请等待刷新完成后再更改存储位置。",
        "training.preferred_move.already_known": "球员已经拥有该个人习惯。",
        "training.preferred_move.not_known": "球员没有该个人习惯。",
    },
    "zh-TW": {
        "error.invalid_request": "要求無效。",
        "error.not_found": "找不到要求的資源。",
        "error.service_unavailable": "本機服務暫時無法使用，請稍後再試。",
        "error.internal_error": "伺服器無法完成要求。",
        "error.numeric_overflow": "提供的數值超出支援範圍。",
        "error.request_failed": "無法完成要求。",
        "error.resource_not_found": "找不到{resource}。",
        "integrity.waiver.mail_not_found": "這項紀律處分已不存在，請重新整理郵件後再試。",
        "integrity.waiver.unavailable": "這項處分已無法透過繳款免除。",
        "integrity.waiver.not_delivered": "這封紀律處分通知尚未送達。",
        "integrity.waiver.expired": "這項處分的繳款期限已過。",
        "integrity.waiver.insufficient_funds": "銀行與投注錢包餘額不足，無法繳納 {amount}。",
        "referee_debt.mail_not_found": "這封裁判追款郵件已不存在，請重新整理郵件後再試。",
        "referee_debt.payment_unavailable": "裁判追款處分已經執行，無法再付款。",
        "referee_debt.payment_expired": "裁判追款的付款期限已過。",
        "referee_debt.insufficient_funds": "銀行與投注錢包餘額不足，無法支付 {amount}。",
        "storage.migration.invalid_target": "所選資料夾包含衝突的 FMODD 資料，或與目前資料目錄重疊。請選擇其他資料夾。",
        "storage.migration.failed": "FMODD 無法複製存檔資料，原始資料未被變更。請選擇其他資料夾或再試一次。",
        "storage.migration.busy": "FMODD 正在重新整理資料。請等待完成後再變更儲存位置。",
        "training.preferred_move.already_known": "球員已經擁有這個慣用動作。",
        "training.preferred_move.not_known": "球員沒有這個慣用動作。",
    },
    "ko-KR": {
        "error.invalid_request": "요청이 올바르지 않습니다.",
        "error.not_found": "요청한 리소스를 찾을 수 없습니다.",
        "error.service_unavailable": "로컬 서비스를 일시적으로 사용할 수 없습니다. 잠시 후 다시 시도하세요.",
        "error.internal_error": "서버에서 요청을 완료할 수 없습니다.",
        "error.numeric_overflow": "입력한 값이 지원 범위를 벗어났습니다.",
        "error.request_failed": "요청을 완료할 수 없습니다.",
        "error.resource_not_found": "{resource}을(를) 찾을 수 없습니다.",
        "integrity.waiver.mail_not_found": "이 징계 처분을 더 이상 찾을 수 없습니다. 메일을 새로 고친 후 다시 시도하세요.",
        "integrity.waiver.unavailable": "이 처분은 더 이상 납부로 면제할 수 없습니다.",
        "integrity.waiver.not_delivered": "이 징계 통지는 아직 전달되지 않았습니다.",
        "integrity.waiver.expired": "이 처분의 납부 기한이 지났습니다.",
        "integrity.waiver.insufficient_funds": "은행과 베팅 지갑 잔액이 부족하여 {amount}을(를) 납부할 수 없습니다.",
        "referee_debt.mail_not_found": "이 심판 지급 요청을 더 이상 찾을 수 없습니다. 메일을 새로 고친 후 다시 시도하세요.",
        "referee_debt.payment_unavailable": "제재가 이미 집행되어 이 심판 지급을 더 이상 완료할 수 없습니다.",
        "referee_debt.payment_expired": "이 심판 지급의 기한이 지났습니다.",
        "referee_debt.insufficient_funds": "은행과 베팅 지갑 잔액이 부족하여 {amount}을(를) 지급할 수 없습니다.",
        "storage.migration.invalid_target": "선택한 폴더에 충돌하는 FMODD 데이터가 있거나 현재 데이터 폴더와 겹칩니다. 다른 폴더를 선택하세요.",
        "storage.migration.failed": "FMODD가 저장 데이터를 복사하지 못했습니다. 원본 데이터는 변경되지 않았습니다. 다른 폴더를 선택하거나 다시 시도하세요.",
        "storage.migration.busy": "FMODD가 데이터를 새로 고치는 중입니다. 완료된 후 저장 위치를 변경하세요.",
        "training.preferred_move.already_known": "선수가 이미 이 개인 습관을 보유하고 있습니다.",
        "training.preferred_move.not_known": "선수에게 이 개인 습관이 없습니다.",
    },
    "de-DE": {
        "error.invalid_request": "Die Anfrage ist ungültig.",
        "error.not_found": "Die angeforderte Ressource wurde nicht gefunden.",
        "error.service_unavailable": "Der lokale Dienst ist vorübergehend nicht verfügbar. Bitte versuchen Sie es erneut.",
        "error.internal_error": "Der Server konnte die Anfrage nicht abschließen.",
        "error.numeric_overflow": "Der angegebene Wert liegt außerhalb des unterstützten Bereichs.",
        "error.request_failed": "Die Anfrage konnte nicht abgeschlossen werden.",
        "error.resource_not_found": "{resource} wurde nicht gefunden.",
        "integrity.waiver.mail_not_found": "Diese Disziplinarstrafe ist nicht mehr verfügbar. Aktualisieren Sie Ihre Nachrichten und versuchen Sie es erneut.",
        "integrity.waiver.unavailable": "Diese Strafe kann nicht mehr durch Zahlung erlassen werden.",
        "integrity.waiver.not_delivered": "Dieser Disziplinarbescheid wurde noch nicht zugestellt.",
        "integrity.waiver.expired": "Die Zahlungsfrist für diese Strafe ist abgelaufen.",
        "integrity.waiver.insufficient_funds": "Ihr Bankkonto und Ihre Wettbörse reichen nicht aus, um {amount} zu zahlen.",
        "referee_debt.mail_not_found": "Diese Zahlungsaufforderung des Schiedsrichters ist nicht mehr verfügbar. Aktualisieren Sie Ihre Nachrichten und versuchen Sie es erneut.",
        "referee_debt.payment_unavailable": "Diese Zahlung an den Schiedsrichter ist nicht mehr möglich, da die Strafe bereits vollstreckt wurde.",
        "referee_debt.payment_expired": "Die Frist für diese Zahlung an den Schiedsrichter ist abgelaufen.",
        "referee_debt.insufficient_funds": "Ihr Bankkonto und Ihre Wettbörse reichen nicht aus, um {amount} zu zahlen.",
        "storage.migration.invalid_target": "Der ausgewählte Ordner enthält widersprüchliche FMODD-Daten oder überschneidet sich mit dem aktuellen Datenordner. Wählen Sie einen anderen Ordner.",
        "storage.migration.failed": "FMODD konnte Ihre Speicherdaten nicht kopieren. Die ursprünglichen Daten wurden nicht verändert. Wählen Sie einen anderen Ordner oder versuchen Sie es erneut.",
        "storage.migration.busy": "FMODD aktualisiert gerade Daten. Warten Sie, bis der Vorgang abgeschlossen ist, und ändern Sie dann den Speicherort.",
        "training.preferred_move.already_known": "Der Spieler beherrscht diese bevorzugte Spielweise bereits.",
        "training.preferred_move.not_known": "Der Spieler beherrscht diese bevorzugte Spielweise nicht.",
    },
    "es-ES": {
        "error.invalid_request": "La solicitud no es válida.",
        "error.not_found": "No se ha encontrado el recurso solicitado.",
        "error.service_unavailable": "El servicio local no está disponible temporalmente. Inténtalo de nuevo.",
        "error.internal_error": "El servidor no ha podido completar la solicitud.",
        "error.numeric_overflow": "El valor proporcionado está fuera del intervalo admitido.",
        "error.request_failed": "No se ha podido completar la solicitud.",
        "error.resource_not_found": "No se ha encontrado {resource}.",
        "integrity.waiver.mail_not_found": "Esta sanción disciplinaria ya no está disponible. Actualiza el correo e inténtalo de nuevo.",
        "integrity.waiver.unavailable": "Esta sanción ya no puede anularse mediante pago.",
        "integrity.waiver.not_delivered": "Esta notificación disciplinaria todavía no ha sido entregada.",
        "integrity.waiver.expired": "El plazo de pago de esta sanción ha vencido.",
        "integrity.waiver.insufficient_funds": "El banco y la cartera de apuestas no tienen fondos suficientes para pagar {amount}.",
        "referee_debt.mail_not_found": "Esta solicitud de pago del árbitro ya no está disponible. Actualiza el correo e inténtalo de nuevo.",
        "referee_debt.payment_unavailable": "Este pago al árbitro ya no puede realizarse porque la sanción ya se ha ejecutado.",
        "referee_debt.payment_expired": "El plazo para realizar este pago al árbitro ha vencido.",
        "referee_debt.insufficient_funds": "El banco y la cartera de apuestas no tienen fondos suficientes para pagar {amount}.",
        "storage.migration.invalid_target": "La carpeta seleccionada contiene datos de FMODD incompatibles o se solapa con la carpeta de datos actual. Elige otra carpeta.",
        "storage.migration.failed": "FMODD no ha podido copiar tus datos guardados. Los datos originales no se han modificado. Elige otra carpeta o inténtalo de nuevo.",
        "storage.migration.busy": "FMODD está actualizando los datos. Espera a que termine y cambia después la ubicación de almacenamiento.",
        "training.preferred_move.already_known": "El jugador ya conoce este movimiento preferido.",
        "training.preferred_move.not_known": "El jugador no conoce este movimiento preferido.",
    },
    "fr-FR": {
        "error.invalid_request": "La requête n’est pas valide.",
        "error.not_found": "La ressource demandée est introuvable.",
        "error.service_unavailable": "Le service local est temporairement indisponible. Réessayez dans un instant.",
        "error.internal_error": "Le serveur n’a pas pu traiter la requête.",
        "error.numeric_overflow": "La valeur fournie se trouve hors de la plage prise en charge.",
        "error.request_failed": "La requête n’a pas pu être traitée.",
        "error.resource_not_found": "{resource} est introuvable.",
        "integrity.waiver.mail_not_found": "Cette sanction disciplinaire n’est plus disponible. Actualisez vos messages et réessayez.",
        "integrity.waiver.unavailable": "Cette sanction ne peut plus être levée par un paiement.",
        "integrity.waiver.not_delivered": "Cette notification disciplinaire n’a pas encore été remise.",
        "integrity.waiver.expired": "Le délai de paiement de cette sanction est dépassé.",
        "integrity.waiver.insufficient_funds": "Votre compte bancaire et votre portefeuille de paris ne permettent pas de payer {amount}.",
        "referee_debt.mail_not_found": "Cette demande de paiement de l’arbitre n’est plus disponible. Actualisez vos messages et réessayez.",
        "referee_debt.payment_unavailable": "Ce paiement à l’arbitre n’est plus possible, car la sanction a déjà été appliquée.",
        "referee_debt.payment_expired": "Le délai de ce paiement à l’arbitre est dépassé.",
        "referee_debt.insufficient_funds": "Votre compte bancaire et votre portefeuille de paris ne permettent pas de payer {amount}.",
        "storage.migration.invalid_target": "Le dossier sélectionné contient des données FMODD incompatibles ou chevauche le dossier de données actuel. Choisissez un autre dossier.",
        "storage.migration.failed": "FMODD n’a pas pu copier vos données enregistrées. Les données d’origine n’ont pas été modifiées. Choisissez un autre dossier ou réessayez.",
        "storage.migration.busy": "FMODD actualise les données. Attendez la fin de l’opération avant de changer l’emplacement de stockage.",
        "training.preferred_move.already_known": "Le joueur connaît déjà ce geste préféré.",
        "training.preferred_move.not_known": "Le joueur ne connaît pas ce geste préféré.",
    },
    "ru-RU": {
        "error.invalid_request": "Недопустимый запрос.",
        "error.not_found": "Запрошенный ресурс не найден.",
        "error.service_unavailable": "Локальная служба временно недоступна. Повторите попытку.",
        "error.internal_error": "Серверу не удалось выполнить запрос.",
        "error.numeric_overflow": "Указанное значение выходит за пределы поддерживаемого диапазона.",
        "error.request_failed": "Не удалось выполнить запрос.",
        "error.resource_not_found": "Ресурс «{resource}» не найден.",
        "integrity.waiver.mail_not_found": "Это дисциплинарное взыскание больше недоступно. Обновите почту и повторите попытку.",
        "integrity.waiver.unavailable": "Это взыскание больше нельзя отменить оплатой.",
        "integrity.waiver.not_delivered": "Это дисциплинарное уведомление ещё не доставлено.",
        "integrity.waiver.expired": "Срок оплаты этого взыскания истёк.",
        "integrity.waiver.insufficient_funds": "На банковском счёте и в кошельке ставок недостаточно средств для оплаты {amount}.",
        "referee_debt.mail_not_found": "Этот запрос на выплату судье больше недоступен. Обновите почту и повторите попытку.",
        "referee_debt.payment_unavailable": "Выплату судье больше нельзя выполнить, поскольку наказание уже применено.",
        "referee_debt.payment_expired": "Срок выплаты судье истёк.",
        "referee_debt.insufficient_funds": "На банковском счёте и в кошельке ставок недостаточно средств для выплаты {amount}.",
        "storage.migration.invalid_target": "Выбранная папка содержит конфликтующие данные FMODD или пересекается с текущей папкой данных. Выберите другую папку.",
        "storage.migration.failed": "FMODD не удалось скопировать сохранённые данные. Исходные данные не изменены. Выберите другую папку или повторите попытку.",
        "storage.migration.busy": "FMODD обновляет данные. Дождитесь завершения, затем измените место хранения.",
        "training.preferred_move.already_known": "Игрок уже владеет этим излюбленным приёмом.",
        "training.preferred_move.not_known": "Игрок не владеет этим излюбленным приёмом.",
    },
    "ja-JP": {
        "error.invalid_request": "リクエストが正しくありません。",
        "error.not_found": "要求されたリソースが見つかりません。",
        "error.service_unavailable": "ローカルサービスは一時的に利用できません。もう一度お試しください。",
        "error.internal_error": "サーバーはリクエストを完了できませんでした。",
        "error.numeric_overflow": "指定された値は対応範囲外です。",
        "error.request_failed": "リクエストを完了できませんでした。",
        "error.resource_not_found": "{resource}が見つかりません。",
        "integrity.waiver.mail_not_found": "この懲戒処分は利用できなくなりました。メールを更新して、もう一度お試しください。",
        "integrity.waiver.unavailable": "この処分は支払いによって免除できなくなりました。",
        "integrity.waiver.not_delivered": "この懲戒通知はまだ届いていません。",
        "integrity.waiver.expired": "この処分の支払期限は過ぎています。",
        "integrity.waiver.insufficient_funds": "銀行口座とベッティングウォレットの残高が不足しているため、{amount}を支払えません。",
        "referee_debt.mail_not_found": "この審判への支払い要求は利用できなくなりました。メールを更新して、もう一度お試しください。",
        "referee_debt.payment_unavailable": "処分がすでに執行されているため、この審判への支払いは完了できません。",
        "referee_debt.payment_expired": "この審判への支払期限は過ぎています。",
        "referee_debt.insufficient_funds": "銀行口座とベッティングウォレットの残高が不足しているため、{amount}を支払えません。",
        "storage.migration.invalid_target": "選択したフォルダーに競合するFMODDデータがあるか、現在のデータフォルダーと重複しています。別のフォルダーを選択してください。",
        "storage.migration.failed": "FMODDは保存データをコピーできませんでした。元のデータは変更されていません。別のフォルダーを選択するか、もう一度お試しください。",
        "storage.migration.busy": "FMODDはデータを更新中です。完了してから保存先を変更してください。",
        "training.preferred_move.already_known": "この選手はすでにこのプレイ特性を習得しています。",
        "training.preferred_move.not_known": "この選手はこのプレイ特性を習得していません。",
    },
    "pt-BR": {
        "error.invalid_request": "A solicitação é inválida.",
        "error.not_found": "O recurso solicitado não foi encontrado.",
        "error.service_unavailable": "O serviço local está temporariamente indisponível. Tente novamente.",
        "error.internal_error": "O servidor não conseguiu concluir a solicitação.",
        "error.numeric_overflow": "O valor informado está fora do intervalo compatível.",
        "error.request_failed": "Não foi possível concluir a solicitação.",
        "error.resource_not_found": "{resource} não foi encontrado.",
        "integrity.waiver.mail_not_found": "Esta penalidade disciplinar não está mais disponível. Atualize as mensagens e tente novamente.",
        "integrity.waiver.unavailable": "Esta penalidade não pode mais ser anulada por pagamento.",
        "integrity.waiver.not_delivered": "Esta notificação disciplinar ainda não foi entregue.",
        "integrity.waiver.expired": "O prazo de pagamento desta penalidade já terminou.",
        "integrity.waiver.insufficient_funds": "O banco e a carteira de apostas não têm saldo suficiente para pagar {amount}.",
        "referee_debt.mail_not_found": "Esta solicitação de pagamento do árbitro não está mais disponível. Atualize as mensagens e tente novamente.",
        "referee_debt.payment_unavailable": "Este pagamento ao árbitro não pode mais ser concluído porque a penalidade já foi aplicada.",
        "referee_debt.payment_expired": "O prazo deste pagamento ao árbitro já terminou.",
        "referee_debt.insufficient_funds": "O banco e a carteira de apostas não têm saldo suficiente para pagar {amount}.",
        "storage.migration.invalid_target": "A pasta selecionada contém dados conflitantes do FMODD ou se sobrepõe à pasta de dados atual. Escolha outra pasta.",
        "storage.migration.failed": "O FMODD não conseguiu copiar seus dados salvos. Os dados originais não foram alterados. Escolha outra pasta ou tente novamente.",
        "storage.migration.busy": "O FMODD está atualizando os dados. Aguarde a conclusão e depois altere o local de armazenamento.",
        "training.preferred_move.already_known": "O jogador já conhece este movimento preferido.",
        "training.preferred_move.not_known": "O jogador não conhece este movimento preferido.",
    },
    "pt-PT": {
        "error.invalid_request": "O pedido é inválido.",
        "error.not_found": "O recurso pedido não foi encontrado.",
        "error.service_unavailable": "O serviço local está temporariamente indisponível. Tente novamente.",
        "error.internal_error": "O servidor não conseguiu concluir o pedido.",
        "error.numeric_overflow": "O valor indicado está fora do intervalo suportado.",
        "error.request_failed": "Não foi possível concluir o pedido.",
        "error.resource_not_found": "{resource} não foi encontrado.",
        "integrity.waiver.mail_not_found": "Esta penalização disciplinar já não está disponível. Atualize as mensagens e tente novamente.",
        "integrity.waiver.unavailable": "Esta penalização já não pode ser anulada através de pagamento.",
        "integrity.waiver.not_delivered": "Esta notificação disciplinar ainda não foi entregue.",
        "integrity.waiver.expired": "O prazo de pagamento desta penalização terminou.",
        "integrity.waiver.insufficient_funds": "O banco e a carteira de apostas não têm saldo suficiente para pagar {amount}.",
        "referee_debt.mail_not_found": "Este pedido de pagamento do árbitro já não está disponível. Atualize as mensagens e tente novamente.",
        "referee_debt.payment_unavailable": "Este pagamento ao árbitro já não pode ser concluído porque a penalização foi aplicada.",
        "referee_debt.payment_expired": "O prazo deste pagamento ao árbitro terminou.",
        "referee_debt.insufficient_funds": "O banco e a carteira de apostas não têm saldo suficiente para pagar {amount}.",
        "storage.migration.invalid_target": "A pasta selecionada contém dados FMODD em conflito ou sobrepõe-se à pasta de dados atual. Escolha outra pasta.",
        "storage.migration.failed": "O FMODD não conseguiu copiar os seus dados guardados. Os dados originais não foram alterados. Escolha outra pasta ou tente novamente.",
        "storage.migration.busy": "O FMODD está a atualizar os dados. Aguarde até terminar e altere depois a localização de armazenamento.",
        "training.preferred_move.already_known": "O jogador já conhece este movimento preferido.",
        "training.preferred_move.not_known": "O jogador não conhece este movimento preferido.",
    },
}


from tools.player_departure_messages import MESSAGES as DEPARTURE_MESSAGES
for _departure_locale, _departure_catalog in DEPARTURE_MESSAGES.items():
    MESSAGE_CATALOGS[_departure_locale].update(_departure_catalog)


ERROR_MESSAGE_KEYS = {
    "invalid_request": "error.invalid_request",
    "not_found": "error.not_found",
    "service_unavailable": "error.service_unavailable",
    "internal_error": "error.internal_error",
    "numeric_overflow": "error.numeric_overflow",
}

STATUS_MESSAGE_KEYS = {
    400: "error.invalid_request",
    404: "error.not_found",
    413: "error.invalid_request",
    503: "error.service_unavailable",
    500: "error.internal_error",
}


MAIL_TEMPLATE_KEYS: dict[str, dict[str, str]] = {
    "bet_profit": {
        "title_key": "mail.type.bet_profit.title",
        "message_key": "mail.type.bet_profit.message",
    },
    "manager_salary": {
        "title_key": "mail.type.manager_salary.title",
        "message_key": "mail.type.manager_salary.message",
    },
    "club_dividend": {
        "title_key": "mail.type.club_dividend.title",
        "message_key": "mail.type.club_dividend.message",
    },
    "bet_schedule_refund": {
        "title_key": "mail.type.schedule_refund.title",
        "message_key": "mail.type.schedule_refund.message",
    },
    "youth_plan_refund": {
        "title_key": "mail.type.youth_refund.title",
        "message_key": "mail.type.youth_refund.message",
    },
    "youth_plan_completed": {
        "title_key": "mail.type.youth_complete.title",
        "message_key": "mail.type.youth_complete.message",
    },
    "virus_rejection": {
        "title_key": "mail.type.virus_rejection.title",
        "message_key": "mail.type.virus_rejection.message",
    },
    "bankruptcy_relief": {
        "title_key": "mail.type.relief.title",
        "message_key": "mail.relief_paid",
    },
    "version_update_reward": {
        "title_key": "mail.type.reward.title",
        "message_key": "mail.type.reward.message",
    },
    "match_integrity": {
        "title_key": "mail.type.integrity.title",
        "message_key": "mail.type.integrity.message",
        "body_key": "mail.type.integrity.body",
    },
    "referee_integrity": {
        "title_key": "mail.type.referee_integrity.title",
        "message_key": "mail.type.referee_integrity.message",
        "body_key": "mail.type.referee_integrity.body",
    },
    "doping_integrity": {
        "title_key": "mail.type.doping_integrity.title",
        "message_key": "mail.type.doping_integrity.message",
        "body_key": "mail.type.doping_integrity.body",
    },
    "referee_apology": {
        "title_key": "mail.type.referee_apology.title",
        "message_key": "mail.type.referee_apology.message",
        "body_key": "mail.type.referee_apology.body",
    },
    "credit_default": {
        "title_key": "mail.type.credit_default.title",
        "message_key": "mail.type.credit_default.message",
    },
    "referee_debt": {
        "title_key": "mail.type.referee_debt.title",
        "message_key": "mail.type.referee_debt.message",
        "body_key": "mail.type.referee_debt.body",
    },
    "sponsorship_offer": {
        "title_key": "mail.type.sponsor_offer.title",
        "message_key": "mail.type.sponsor_offer.message",
    },
    "sponsorship_contract": {
        "title_key": "mail.type.sponsor_contract.title",
        "message_key": "mail.type.sponsor_contract.message",
    },
    "sponsorship_payment": {
        "title_key": "mail.type.sponsor_payment.title",
        "message_key": "mail.type.sponsor_payment.message",
    },
}


def mail_template_fields(
    mail_type: str, params: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Return stable localization metadata for a newly-created mail record.

    Producers keep their rendered legacy fields for older clients.  New clients
    replay the semantic keys with locale-aware formatting, while records that
    predate this contract remain untouched and continue to use their stored text.
    """
    keys = MAIL_TEMPLATE_KEYS.get(str(mail_type or ""), {})
    if not keys:
        return {}
    result: dict[str, Any] = {
        **keys,
        "template_version": 1,
        "template_params": dict(params or {}),
    }
    if "message_key" in keys:
        result["template_key"] = keys["message_key"]
    return result


def normalize_locale(locale: str | None) -> str:
    """Return a supported locale, accepting common HTTP language forms."""
    requested = str(locale or "").split(",", 1)[0].split(";", 1)[0].strip()
    if not requested:
        return DEFAULT_LOCALE
    requested_lower = requested.replace("_", "-").lower()
    if requested_lower.startswith(("zh-tw", "zh-hant")):
        return "zh-TW"
    if requested_lower.startswith(("zh-cn", "zh-hans")):
        return "zh-CN"
    for supported in SUPPORTED_LOCALES:
        if requested_lower == supported.lower():
            return supported
    language = requested_lower.split("-", 1)[0]
    for supported in SUPPORTED_LOCALES:
        if language == supported.split("-", 1)[0].lower():
            return supported
    return DEFAULT_LOCALE


def message_key_for_error(error_code: str | None) -> str:
    """Return the established template key without deriving text from errors."""
    return ERROR_MESSAGE_KEYS.get(str(error_code or ""), "error.request_failed")


def message_key_for_status(status: int) -> str:
    """Return a safe generic message for a legacy HTTP error response."""
    return STATUS_MESSAGE_KEYS.get(int(status), "error.request_failed")


@dataclass(frozen=True)
class LocalizedMessage:
    """A semantic user message that can be rendered on the server or client."""

    key: str
    params: Mapping[str, Any] = field(default_factory=dict)

    def render(self, locale: str | None = None) -> str:
        catalog = MESSAGE_CATALOGS.get(normalize_locale(locale), {})
        template = catalog.get(self.key) or MESSAGE_CATALOGS[DEFAULT_LOCALE].get(
            self.key,
            MESSAGE_CATALOGS[DEFAULT_LOCALE]["error.request_failed"],
        )
        try:
            return template.format_map(dict(self.params))
        except (KeyError, ValueError, TypeError):
            # Template failures must not surface implementation details.
            return template

    def payload(self, locale: str | None = None) -> dict[str, Any]:
        return {
            "message": self.render(locale),
            "message_key": self.key,
            "message_params": dict(self.params),
        }


def localized_error_payload(
    payload: Mapping[str, Any], *, locale: str | None = None,
    message_key: str | None = None,
    message_params: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Add the non-breaking localized-message contract to an error payload.

    ``error`` and all diagnostic fields are deliberately retained verbatim for
    existing callers.  The new ``message`` is rendered only from catalogued
    keys and caller-supplied factual parameters, never from exception text.
    """
    result = dict(payload)
    # Older DomainError instances may not have supplied a phase.  HTTP clients
    # can nevertheless rely on the new structured payload always carrying one.
    result.setdefault("error_phase", "unknown")
    key = str(message_key or result.get("message_key") or "").strip()
    if not key:
        key = message_key_for_error(str(result.get("error_code") or ""))
    params = message_params
    if params is None:
        existing_params = result.get("message_params")
        params = existing_params if isinstance(existing_params, Mapping) else {}
    result.update(LocalizedMessage(key, params).payload(locale))
    return result
