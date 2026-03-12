self.addEventListener('push', function(event) {
    const data = event.data ? event.data.json() : { title: 'Новое сообщение', body: 'Зайдите в чат' };
    event.waitUntil(
        self.registration.showNotification(data.title, {
            body: data.body,
            icon: 'https://i.ibb.co/4pSbxsh/user-avatar.png'
        })
    );
});
