$(function() {
    function refreshData() {
        let container = $('#console');
        $.get('/snipswatch/refreshConsole', function(data) {
            for (let i = 0; i < data.data.length; i++) {
                container.append(
                    '<span class="logLine">' + data.data[i] + '</span>'
                );
            }
        }).always(function(data) {
            if ($('#checkedCheckbox').is(':visible')) {
                container.scrollTop(container.prop('scrollHeight'))
            }
        });
    }

    $('#checkedCheckbox').on('click', function() {
        $(this).hide();
        $('#emptyCheckbox').show();
    });

    $('#emptyCheckbox').on('click', function() {
        $(this).hide();
        $('#checkedCheckbox').show();
    });

    $('[class^="fas fa-thermometer"]').on('click', function() {
        $('[class^="fas fa-thermometer"]').removeClass('snipswatchActiveVerbosity');
        $(this).addClass('snipswatchActiveVerbosity');
        $.ajax({
            url: '/snipswatch/verbosity',
            data: {
                verbosity: $(this).data('verbosity')
            },
            type: 'POST'
        })
    });

    setInterval(function() {
        refreshData()
    }, 500)
});