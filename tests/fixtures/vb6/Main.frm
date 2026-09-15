VERSION 5.00
Begin VB.Form Main
   Caption = "Sample"
   Begin VB.CommandButton cmdSave
      Caption = "Save"
      BeginProperty Font
         Name = "Arial"
         Size = 8.25
      EndProperty
   End
End
Attribute VB_Name = "Main"
Option Explicit

Private Sub Form_Load()
    RefreshView
End Sub

Private Sub cmdSave_Click()
    Dim customer As Customer
    Set customer = New Customer
    customer.Save
    RefreshView
End Sub

Private Sub RefreshView()
    cmdSave.Caption = "Ready"
End Sub
